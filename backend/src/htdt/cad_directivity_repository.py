from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import sqlite3

from pydantic import BaseModel, ConfigDict, Field

from .cad_directivity import (
    DirectivityDataset,
    validate_directivity_dataset_binding,
)
from .cad_directivity_import import replay_directivity_import
from .cad_equipment import DirectivityDataFormat
from .cad_equipment_repository import CadEquipmentRepository
from .cad_repository import SceneRepository
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
    connect_sqlite,

)
from .managed_assets import MANAGED_ASSETS_DIRNAME, ManagedAssetStore


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DirectivitySourceAssetMetadata(BaseModel):
    """Import-context metadata for one immutable directivity source asset.

    Byte identity lives in the shared managed asset registry
    (``cad_measurement_assets`` row plus the digest-named file); this record
    preserves the original filename/media/source-format context separately
    so evidence metadata never alters the content address.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    source_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    filename: str | None = Field(default=None, min_length=1)
    media_type: str | None = Field(default=None, min_length=1)
    source_format: DirectivityDataFormat
    declared_schema: str | None = Field(default=None, min_length=1)
    size_bytes: int = Field(ge=0)
    recorded_at_utc: str = Field(min_length=1)


class CadDirectivityRepository:
    """Append-only persistence for exact O100C DirectivityDataset authority.

    Every persisted non-analytic dataset is bound to an immutable
    content-addressed source asset: the exact imported bytes are stored once
    in the shared managed asset store under their SHA-256 (the same
    ``measurement-assets`` infrastructure and native backup contract used by
    measurement and treatment evidence), size/hash are verified on write and
    on every read, and the recorded adapter version must replay the dataset
    from those exact bytes. Missing, tampered, unregistered-adapter or
    non-replaying evidence fails closed instead of serving normalized data
    whose import transformation cannot be re-audited.

    Analytic directivity stays asset-free by design: an analytic capability
    carries no ``data_asset_sha256``, so the dataset binding itself rejects
    it before any asset lookup happens.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        equipment_repository: CadEquipmentRepository | None = None,
        assets_dir: Path | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.equipment_repository = (
            equipment_repository
            if equipment_repository is not None
            else CadEquipmentRepository(scene_repository)
        )
        self.path = Path(scene_repository.path)
        # Resolve any interrupted managed-data restore before the asset store
        # can create its directory inside the managed data root.
        ensure_native_schema(self.path)
        self.assets_dir = (
            Path(assets_dir)
            if assets_dir is not None
            else self.path.parent / MANAGED_ASSETS_DIRNAME
        )
        self._asset_store = ManagedAssetStore(self.assets_dir)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_directivity_datasets', 'cad_measurement_assets', 'cad_directivity_source_assets')

    def _verified_asset_file(
        self,
        digest: str,
        *,
        relative_path: str,
        size_bytes: int,
    ) -> bytes:
        """Reopen a registered managed asset, failing closed on tampering."""
        root = self.path.parent.resolve()
        target = (self.path.parent / relative_path).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                'managed directivity source asset escapes the data root'
            ) from exc
        if target.is_symlink() or not target.is_file():
            raise ValueError('managed directivity source asset is missing')
        if target.stat().st_size != size_bytes:
            raise ValueError('managed directivity source asset size mismatch')
        raw = self._asset_store.read_file(target)
        if sha256(raw).hexdigest() != digest:
            raise ValueError(
                'managed directivity source asset SHA-256 mismatch'
            )
        return raw

    def _verified_source_asset(self, digest: str) -> bytes:
        """Reopen the exact bound source bytes for *digest*.

        A dataset whose source asset is not registered, or whose managed
        file is missing or fails the size/SHA-256 contract, fails closed —
        the normalized payload is never served without its exact evidence.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT relative_path, size_bytes
                FROM cad_measurement_assets
                WHERE sha256=?
                """,
                (digest,),
            ).fetchone()
        if row is None:
            raise ValueError(
                'DirectivityDataset source asset is not registered in the '
                'managed asset store'
            )
        return self._verified_asset_file(
            digest,
            relative_path=row['relative_path'],
            size_bytes=int(row['size_bytes']),
        )

    def read_source_asset(self, source_asset_sha256: str) -> bytes | None:
        """Reopen the exact original source bytes for a content address.

        Returns ``None`` only when no managed asset is registered for the
        digest; a registered asset whose file is missing, resized or
        tampered raises instead of returning unverifiable bytes.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT relative_path, size_bytes
                FROM cad_measurement_assets
                WHERE sha256=?
                """,
                (source_asset_sha256,),
            ).fetchone()
        if row is None:
            return None
        return self._verified_asset_file(
            source_asset_sha256,
            relative_path=row['relative_path'],
            size_bytes=int(row['size_bytes']),
        )

    def get_source_metadata(
        self,
        source_asset_sha256: str,
    ) -> DirectivitySourceAssetMetadata | None:
        """Return preserved filename/media/format metadata for an asset.

        The bound managed file is re-verified before metadata is served, so
        tampered evidence never comes back with clean provenance.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT a.filename, a.media_type, a.source_format,
                       a.declared_schema, a.recorded_at_utc,
                       m.size_bytes, m.relative_path
                FROM cad_directivity_source_assets a
                JOIN cad_measurement_assets m
                    ON m.sha256 = a.source_asset_sha256
                WHERE a.source_asset_sha256=?
                """,
                (source_asset_sha256,),
            ).fetchone()
        if row is None:
            return None
        self._verified_asset_file(
            source_asset_sha256,
            relative_path=row['relative_path'],
            size_bytes=int(row['size_bytes']),
        )
        return DirectivitySourceAssetMetadata(
            source_asset_sha256=source_asset_sha256,
            filename=row['filename'],
            media_type=row['media_type'],
            source_format=row['source_format'],
            declared_schema=row['declared_schema'],
            size_bytes=int(row['size_bytes']),
            recorded_at_utc=row['recorded_at_utc'],
        )

    def _replay_verified_dataset(
        self,
        dataset: DirectivityDataset,
    ) -> DirectivityDataset:
        """Authoritative read: re-verify the persisted import binding.

        The bound EquipmentDefinition, the managed source asset and the
        recorded adapter version must all resolve and reproduce the
        persisted dataset exactly; anything less fails closed.
        """
        definition = self.equipment_repository.get_definition_by_hash(
            dataset.equipment_definition_sha256
        )
        if definition is None:
            raise ValueError(
                'persisted DirectivityDataset references missing EquipmentDefinition'
            )
        validate_directivity_dataset_binding(dataset, definition)
        source_bytes = self._verified_source_asset(dataset.source_asset_sha256)
        replayed = replay_directivity_import(
            source_bytes=source_bytes,
            equipment_definition=definition,
            adapter_id=dataset.adapter_id,
            adapter_version=dataset.adapter_version,
        )
        if replayed != dataset:
            raise ValueError(
                'persisted DirectivityDataset does not replay from its '
                'bound source asset'
            )
        return dataset

    def _decode_dataset_row(self, row: sqlite3.Row) -> DirectivityDataset:
        dataset = DirectivityDataset.model_validate_json(row['payload_json'])
        if dataset.semantic_sha256 != row['semantic_sha256']:
            raise ValueError(
                'persisted DirectivityDataset semantic hash column mismatch'
            )
        return self._replay_verified_dataset(dataset)

    def save_dataset(
        self,
        dataset: DirectivityDataset,
        *,
        source_bytes: bytes | None = None,
        source_filename: str | None = None,
        media_type: str | None = None,
        declared_schema: str | None = None,
    ) -> DirectivityDataset:
        """Persist a dataset bound to its exact imported source asset.

        ``source_bytes`` are the exact imported bytes the dataset was
        normalized from: they must hash to
        ``dataset.source_asset_sha256``, are installed atomically into the
        managed asset store (identical bytes deduplicate onto one asset),
        and the recorded adapter version must replay the dataset from them
        before anything is committed. When ``source_bytes`` is omitted the
        dataset must bind to an already-installed managed asset — it is
        re-verified and replayed the same way — so a dataset can never be
        persisted as a hash without evidence behind it.
        """
        dataset = DirectivityDataset.model_validate(
            dataset.model_dump(mode='python')
        )
        definition = self.equipment_repository.get_definition_by_hash(
            dataset.equipment_definition_sha256
        )
        if definition is None:
            raise ValueError(
                'DirectivityDataset references an unpersisted EquipmentDefinition'
            )
        validate_directivity_dataset_binding(dataset, definition)

        digest = dataset.source_asset_sha256
        if source_bytes is not None:
            if not isinstance(source_bytes, bytes):
                raise TypeError('directivity source asset payload must be bytes')
            if sha256(source_bytes).hexdigest() != digest:
                raise ValueError(
                    'directivity source bytes do not match '
                    'DirectivityDataset.source_asset_sha256'
                )
            if not source_filename:
                raise ValueError(
                    'directivity source filename is required when persisting '
                    'source bytes'
                )
            # Install before the transaction: a failed commit leaves a safe
            # content-addressed orphan rather than a partially written file.
            self._asset_store.ensure_installed(digest, source_bytes)
            bound_source = source_bytes
        else:
            bound_source = self._verified_source_asset(digest)

        replayed = replay_directivity_import(
            source_bytes=bound_source,
            equipment_definition=definition,
            adapter_id=dataset.adapter_id,
            adapter_version=dataset.adapter_version,
        )
        if replayed != dataset:
            raise ValueError(
                'DirectivityDataset does not replay from its bound source asset'
            )

        target = self._asset_store.asset_path(digest)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_directivity_datasets
                WHERE dataset_id=? AND version=?
                """,
                (dataset.dataset_id, dataset.version),
            ).fetchone()
            persisted: DirectivityDataset | None = None
            if existing is not None:
                persisted = DirectivityDataset.model_validate_json(
                    existing['payload_json']
                )
                if persisted != dataset:
                    raise ValueError(
                        'directivity dataset id/version already exists with different semantics'
                    )
            connection.execute(
                """
                INSERT OR IGNORE INTO cad_measurement_assets(
                    sha256, filename, relative_path, size_bytes
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    digest,
                    source_filename or '',
                    str(target.relative_to(self.path.parent)),
                    len(bound_source),
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO cad_directivity_source_assets(
                    source_asset_sha256, filename, media_type,
                    source_format, declared_schema, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    digest,
                    source_filename,
                    media_type,
                    dataset.source_format,
                    declared_schema,
                    _utc_now(),
                ),
            )
            if persisted is None:
                connection.execute(
                    """
                    INSERT INTO cad_directivity_datasets(
                        dataset_id, version,
                        equipment_definition_sha256,
                        source_asset_sha256,
                        semantic_sha256,
                        payload_json,
                        recorded_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        dataset.dataset_id,
                        dataset.version,
                        dataset.equipment_definition_sha256,
                        dataset.source_asset_sha256,
                        dataset.semantic_sha256,
                        dataset.model_dump_json(),
                        _utc_now(),
                    ),
                )
        return persisted if persisted is not None else dataset

    def get_dataset(
        self,
        dataset_id: str,
        version: str,
    ) -> DirectivityDataset | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT semantic_sha256, payload_json
                FROM cad_directivity_datasets
                WHERE dataset_id=? AND version=?
                """,
                (dataset_id, version),
            ).fetchone()
        if row is None:
            return None
        return self._decode_dataset_row(row)

    def get_dataset_by_hash(
        self,
        semantic_sha256: str,
    ) -> DirectivityDataset | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT semantic_sha256, payload_json
                FROM cad_directivity_datasets
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        if row is None:
            return None
        return self._decode_dataset_row(row)

    def list_datasets_for_definition(
        self,
        equipment_definition_sha256: str,
    ) -> tuple[DirectivityDataset, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT semantic_sha256, payload_json
                FROM cad_directivity_datasets
                WHERE equipment_definition_sha256=?
                ORDER BY seq ASC
                """,
                (equipment_definition_sha256,),
            ).fetchall()
        return tuple(self._decode_dataset_row(row) for row in rows)
