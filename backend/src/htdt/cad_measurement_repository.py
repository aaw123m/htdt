from __future__ import annotations

from contextlib import closing
from array import array
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from .cad_measurement_models import (
    CadFrequencyResponseDataset,
    CadMeasurementComparison,
    CadMeasurementRecord,
    build_measurement_comparison,
    replay_measurement_comparison,
)
from .cad_measurement_quality import dataset_sha256
from .cad_measurements import import_transformation_sha256, verify_imported_dataset
from .cad_repository import SceneRepository, SceneRevision
from .cad_scene import acoustic_reference_position, scene_content_hash
from .cad_search_repository import CadSearchRepository
from .comparison import ComparisonResult, FrequencyResponse, replay_comparison_result
from .managed_assets import (
    MANAGED_ASSETS_DIRNAME,
    ManagedAssetError,
    ManagedAssetStore,
    verify_managed_asset,
)


class MeasurementPlanConflictError(ValueError):
    """A measurement-plan save violated the plan_id single-head lifecycle contract."""


@dataclass(frozen=True)
class VerifiedMeasurementAsset:
    """A managed raw measurement asset that proved its full storage contract.

    Produced by ``CadMeasurementRepository.validate_raw_asset*`` after the
    ``cad_measurement_assets`` row keyed by the content-addressed digest was
    resolved and the declared file passed the shared managed asset contract:
    safe relative path contained under the managed assets directory, a
    content-addressed leaf name, an existing regular file (never a symlink),
    the stored size and a streamed SHA-256 equal to the digest.
    """

    sha256: str
    filename: str
    relative_path: str
    size_bytes: int
    path: Path


@dataclass(frozen=True)
class MeasurementEvidenceBundle:
    """Authoritative raw-backed evidence for one persisted measurement.

    ``dataset`` passed every authoritative read check — persisted semantic
    hash, versioned transformation seal, the managed raw-asset contract and
    the pinned importer replay — and ``raw_asset`` is the exact verified
    content-addressed source the dataset rederives from. Consumers that
    treat measurement data as production evidence (quality reports, O60
    validation, calibration, O100G lifecycle, robustness decisions) resolve
    through this bundle, or through the equally verified ``get_dataset`` /
    ``dataset_for_measurement`` reads, instead of trusting a stored SHA.
    """

    measurement: CadMeasurementRecord
    dataset: CadFrequencyResponseDataset
    raw_asset: VerifiedMeasurementAsset


def _pack(values: tuple[float, ...] | None) -> bytes | None:
    if values is None:
        return None
    payload = array('d', values)
    if payload.itemsize != 8:
        raise RuntimeError('unexpected double size')
    if os.sys.byteorder != 'little':
        payload.byteswap()
    return payload.tobytes()


def _unpack(blob: bytes | None) -> tuple[float, ...] | None:
    if blob is None:
        return None
    payload = array('d')
    payload.frombytes(blob)
    if os.sys.byteorder != 'little':
        payload.byteswap()
    return tuple(payload)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CadMeasurementRepository:
    """Native measurement storage bound directly to immutable SceneRevision rows."""

    def __init__(self, scene_repository: SceneRepository, assets_dir: Path | None = None) -> None:
        self.scene_repository = scene_repository
        self.search_repository = CadSearchRepository(scene_repository)
        self.path = scene_repository.path
        self.assets_dir = Path(assets_dir) if assets_dir is not None else self.path.parent / MANAGED_ASSETS_DIRNAME
        self._asset_store = ManagedAssetStore(self.assets_dir)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                '''
                CREATE TABLE IF NOT EXISTS cad_measurement_assets (
                    sha256 TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cad_measurements (
                    measurement_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    scene_revision_id TEXT NOT NULL REFERENCES scene_revisions(revision_id),
                    scene_content_hash TEXT NOT NULL,
                    measurement_entity_id TEXT NOT NULL,
                    measurement_position_json TEXT NOT NULL,
                    measurement_direction_json TEXT,
                    evidence_type TEXT NOT NULL,
                    channel_role TEXT NOT NULL,
                    source_speaker_ids_json TEXT NOT NULL,
                    radiation_scope TEXT NOT NULL,
                    routing_evidence TEXT NOT NULL,
                    captured_at TEXT,
                    imported_at TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    external_source_id TEXT,
                    quality_status TEXT NOT NULL,
                    quality_reasons_json TEXT NOT NULL,
                    quality_source TEXT NOT NULL,
                    provenance_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cad_measurements_document_imported
                    ON cad_measurements(document_id, imported_at DESC);
                CREATE INDEX IF NOT EXISTS idx_cad_measurements_revision
                    ON cad_measurements(scene_revision_id);
                CREATE TABLE IF NOT EXISTS cad_frequency_responses (
                    dataset_id TEXT PRIMARY KEY,
                    measurement_id TEXT NOT NULL UNIQUE REFERENCES cad_measurements(measurement_id),
                    frequency_blob BLOB NOT NULL,
                    level_blob BLOB NOT NULL,
                    phase_blob BLOB,
                    phase_status TEXT NOT NULL,
                    level_reference TEXT NOT NULL,
                    smoothing TEXT,
                    processing_json TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL REFERENCES cad_measurement_assets(sha256),
                    importer_version TEXT NOT NULL,
                    dataset_sha256 TEXT,
                    transformation_sha256 TEXT
                );
                CREATE TABLE IF NOT EXISTS cad_measurement_comparisons (
                    comparison_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    dataset_a_id TEXT NOT NULL REFERENCES cad_frequency_responses(dataset_id),
                    dataset_b_id TEXT NOT NULL REFERENCES cad_frequency_responses(dataset_id),
                    scene_revision_a_id TEXT NOT NULL REFERENCES scene_revisions(revision_id),
                    scene_revision_b_id TEXT NOT NULL REFERENCES scene_revisions(revision_id),
                    created_at TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cad_measurement_comparisons_document_created
                    ON cad_measurement_comparisons(document_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS cad_measurement_plans (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    search_spec_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    applied_scene_revision_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    plan_sha256 TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(applied_scene_revision_id) REFERENCES scene_revisions(revision_id)
                );
                CREATE INDEX IF NOT EXISTS idx_cad_measurement_plans_search_seq
                    ON cad_measurement_plans(search_spec_id, seq ASC);
                '''
            )
            # Import-transformation binding migration: rows written before the
            # persisted dataset semantic hash / transformation seal existed
            # keep NULL and are non-authoritative — reads fail closed
            # (``_row_to_dataset``) rather than fabricating an identity this
            # version never attested. Both are lazy optional columns in the
            # pre-versioning table signature.
            columns = {
                row['name']
                for row in connection.execute('PRAGMA table_info(cad_frequency_responses)')
            }
            if 'dataset_sha256' not in columns:
                connection.execute(
                    'ALTER TABLE cad_frequency_responses ADD COLUMN dataset_sha256 TEXT'
                )
            if 'transformation_sha256' not in columns:
                connection.execute(
                    'ALTER TABLE cad_frequency_responses ADD COLUMN transformation_sha256 TEXT'
                )

    def _validated_revision(self, record: CadMeasurementRecord) -> SceneRevision:
        revision = self.scene_repository.get(record.scene_revision_id)
        if revision is None:
            raise ValueError(f'unknown scene revision: {record.scene_revision_id}')
        if revision.document_id != record.document_id:
            raise ValueError('measurement scene revision belongs to a different document')
        if revision.content_hash != record.scene_content_hash:
            raise ValueError('measurement scene content hash does not match revision')
        try:
            entity = revision.document.entity(record.measurement_entity_id)
        except KeyError as exc:
            raise ValueError('measurement entity does not exist in source revision') from exc
        position = acoustic_reference_position(entity)
        if position is None:
            raise ValueError('measurement entity has no acoustic reference position')
        if position != record.measurement_position:
            raise ValueError('measurement position snapshot does not match source revision')
        for source_id in record.source_speaker_ids:
            try:
                source = revision.document.entity(source_id)
            except KeyError as exc:
                raise ValueError(f'source speaker missing from source revision: {source_id}') from exc
            if source.kind != 'speaker':
                raise ValueError(f'source entity is not a speaker: {source_id}')
        return revision

    def _asset_path(self, digest: str) -> Path:
        return self._asset_store.asset_path(digest)

    def save(
        self,
        record: CadMeasurementRecord,
        dataset: CadFrequencyResponseDataset,
        *,
        raw_filename: str,
        raw_bytes: bytes,
    ) -> None:
        """Persist one measurement record and its FR dataset as bound evidence.

        Beyond the SceneRevision/entity binding and the raw-asset SHA check,
        the persisted samples must be the canonical output of the dataset's
        declared versioned importer: ``verify_imported_dataset`` resolves the
        importer authority, requires its source kind to equal the record's,
        and reruns the exact transformation against ``raw_bytes`` so the
        stored arrays — not just the raw file — are bound to the source
        bytes. The row is then sealed with the immutable dataset semantic
        hash (``dataset_sha256``) and the versioned transformation seal
        (``transformation_sha256`` over raw source + importer + dataset
        hash), which reads re-verify.
        """
        self._validated_revision(record)
        if dataset.measurement_id != record.measurement_id:
            raise ValueError('dataset measurement_id does not match measurement record')
        digest = sha256(raw_bytes).hexdigest()
        if digest != dataset.source_sha256:
            raise ValueError('raw asset SHA-256 does not match dataset source_sha256')
        verify_imported_dataset(dataset, raw_bytes, source_kind=record.source_kind)
        dataset_identity = dataset_sha256(dataset)
        transformation_identity = import_transformation_sha256(
            source_sha256=digest,
            importer_version=dataset.importer_version,
            dataset_sha256=dataset_identity,
        )
        target = self._asset_path(digest)
        # An already-installed identical digest is a successful dedup hit;
        # anything else at the path is corrupt or a genuine collision.
        self._asset_store.ensure_installed(digest, raw_bytes)

        # A failed transaction leaves the installed digest path in place: the
        # content-addressed file may already be referenced by another committed
        # row, and an orphaned asset is safer than deleting referenced data.
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute(
                'SELECT 1 FROM cad_measurements WHERE measurement_id=?',
                (record.measurement_id,),
            ).fetchone() is not None:
                raise ValueError(f'measurement already exists: {record.measurement_id}')
            if connection.execute(
                'SELECT 1 FROM cad_frequency_responses WHERE dataset_id=?',
                (dataset.dataset_id,),
            ).fetchone() is not None:
                raise ValueError(f'dataset already exists: {dataset.dataset_id}')
            connection.execute(
                '''INSERT OR IGNORE INTO cad_measurement_assets(
                    sha256, filename, relative_path, size_bytes
                ) VALUES (?, ?, ?, ?)''',
                (digest, raw_filename, str(target.relative_to(self.path.parent)), len(raw_bytes)),
            )
            connection.execute(
                '''INSERT INTO cad_measurements(
                    measurement_id, document_id, scene_revision_id, scene_content_hash,
                    measurement_entity_id, measurement_position_json, measurement_direction_json,
                    evidence_type, channel_role, source_speaker_ids_json, radiation_scope,
                    routing_evidence, captured_at, imported_at, source_kind, external_source_id,
                    quality_status, quality_reasons_json, quality_source, provenance_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    record.measurement_id,
                    record.document_id,
                    record.scene_revision_id,
                    record.scene_content_hash,
                    record.measurement_entity_id,
                    record.measurement_position.model_dump_json(),
                    None if record.measurement_direction is None else record.measurement_direction.model_dump_json(),
                    record.evidence_type,
                    record.channel_role,
                    json.dumps(record.source_speaker_ids, ensure_ascii=False, separators=(',', ':')),
                    record.radiation_scope,
                    record.routing_evidence,
                    record.captured_at,
                    record.imported_at,
                    record.source_kind,
                    record.external_source_id,
                    record.quality_status,
                    json.dumps(record.quality_reasons, ensure_ascii=False, separators=(',', ':')),
                    record.quality_source,
                    record.provenance_json,
                ),
            )
            connection.execute(
                '''INSERT INTO cad_frequency_responses(
                    dataset_id, measurement_id, frequency_blob, level_blob, phase_blob,
                    phase_status, level_reference, smoothing, processing_json,
                    source_sha256, importer_version, dataset_sha256,
                    transformation_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    dataset.dataset_id,
                    dataset.measurement_id,
                    _pack(dataset.frequency_hz),
                    _pack(dataset.level_db),
                    _pack(dataset.phase_deg),
                    dataset.phase_status,
                    dataset.level_reference,
                    dataset.smoothing,
                    dataset.processing_json,
                    dataset.source_sha256,
                    dataset.importer_version,
                    dataset_identity,
                    transformation_identity,
                ),
            )
            connection.commit()

    def get_measurement(self, measurement_id: str) -> CadMeasurementRecord | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_measurements WHERE measurement_id=?',
                (measurement_id,),
            ).fetchone()
        return None if row is None else self._row_to_measurement(row)

    _DATASET_SELECT = (
        '''SELECT d.*, m.source_kind AS record_source_kind
           FROM cad_frequency_responses d
           LEFT JOIN cad_measurements m ON m.measurement_id=d.measurement_id'''
    )

    def get_dataset(self, dataset_id: str) -> CadFrequencyResponseDataset | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                f'{self._DATASET_SELECT} WHERE d.dataset_id=?',
                (dataset_id,),
            ).fetchone()
        return None if row is None else self._row_to_dataset(row)

    def dataset_for_measurement(self, measurement_id: str) -> CadFrequencyResponseDataset | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                f'{self._DATASET_SELECT} WHERE d.measurement_id=?',
                (measurement_id,),
            ).fetchone()
        return None if row is None else self._row_to_dataset(row)

    def list_measurements(self, document_id: str) -> tuple[CadMeasurementRecord, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM cad_measurements WHERE document_id=? ORDER BY imported_at DESC, measurement_id',
                (document_id,),
            ).fetchall()
        return tuple(self._row_to_measurement(row) for row in rows)

    def source_revision(self, measurement_id: str) -> SceneRevision:
        record = self.get_measurement(measurement_id)
        if record is None:
            raise KeyError(measurement_id)
        return self._validated_revision(record)

    @staticmethod
    def _comparison_evidence_row(
        connection: sqlite3.Connection,
        dataset_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            '''SELECT d.*, m.document_id, m.scene_revision_id,
                      m.source_kind AS record_source_kind
               FROM cad_frequency_responses d
               JOIN cad_measurements m ON m.measurement_id=d.measurement_id
               WHERE d.dataset_id=?''',
            (dataset_id,),
        ).fetchone()

    def save_comparison(
        self,
        dataset_a_id: str,
        dataset_b_id: str,
        result: ComparisonResult,
    ) -> CadMeasurementComparison:
        """Persist a comparison only if it replays exactly from bound datasets.

        The caller-supplied result is never trusted on its own: the two exact
        persisted datasets are loaded, the pinned algorithm is rerun against
        them under the declared spec, and the record is sealed with the
        dataset semantic hashes, the comparison-spec hash and a comparison
        identity SHA-256. Any mismatch fails closed before the INSERT.
        """
        if dataset_a_id == dataset_b_id:
            raise ValueError('comparison requires two different datasets')
        if not isinstance(result, ComparisonResult):
            raise TypeError('comparison result must be a ComparisonResult')
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            rows = []
            for dataset_id in (dataset_a_id, dataset_b_id):
                row = self._comparison_evidence_row(connection, dataset_id)
                if row is None:
                    raise KeyError(f'dataset not found: {dataset_id}')
                rows.append(row)
            if rows[0]['document_id'] != rows[1]['document_id']:
                raise ValueError('comparison datasets belong to different documents')
            dataset_a = self._row_to_dataset(rows[0])
            dataset_b = self._row_to_dataset(rows[1])
            recomputed = replay_comparison_result(
                result,
                FrequencyResponse(dataset_a.frequency_hz, dataset_a.level_db),
                FrequencyResponse(dataset_b.frequency_hz, dataset_b.level_db),
            )
            if recomputed != result:
                raise ValueError(
                    'comparison result does not match the canonical algorithm '
                    'output for the bound datasets'
                )
            comparison = build_measurement_comparison(
                comparison_id=str(uuid4()),
                document_id=rows[0]['document_id'],
                dataset_a_id=dataset_a_id,
                dataset_b_id=dataset_b_id,
                dataset_a_sha256=dataset_sha256(dataset_a),
                dataset_b_sha256=dataset_sha256(dataset_b),
                scene_revision_a_id=rows[0]['scene_revision_id'],
                scene_revision_b_id=rows[1]['scene_revision_id'],
                created_at=_utc_now(),
                result=result,
            )
            result_payload = {
                **asdict(result),
                'dataset_a_sha256': comparison.dataset_a_sha256,
                'dataset_b_sha256': comparison.dataset_b_sha256,
                'algorithm_sha256': comparison.algorithm_sha256,
                'spec_sha256': comparison.spec_sha256,
                'comparison_sha256': comparison.comparison_sha256,
            }
            connection.execute(
                '''INSERT INTO cad_measurement_comparisons(
                    comparison_id, document_id, dataset_a_id, dataset_b_id,
                    scene_revision_a_id, scene_revision_b_id, created_at, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    comparison.comparison_id,
                    comparison.document_id,
                    comparison.dataset_a_id,
                    comparison.dataset_b_id,
                    comparison.scene_revision_a_id,
                    comparison.scene_revision_b_id,
                    comparison.created_at,
                    json.dumps(result_payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False),
                ),
            )
            connection.commit()
        return comparison

    def _validate_current_comparison(
        self,
        comparison: CadMeasurementComparison,
    ) -> CadMeasurementComparison:
        """Re-verify a persisted comparison against current bound evidence.

        Authoritative reads reload the exact datasets, check the semantic
        dataset hashes and revision bindings, then replay the pinned
        algorithm and require exact equality with the persisted result. A row
        whose evidence was altered or removed fails closed instead of
        silently serving stale comparison values.
        """
        with closing(self._connect()) as connection:
            evidence = []
            for dataset_id in (comparison.dataset_a_id, comparison.dataset_b_id):
                row = self._comparison_evidence_row(connection, dataset_id)
                if row is None:
                    raise ValueError(
                        f'comparison references unknown dataset: {dataset_id}'
                    )
                evidence.append(
                    (
                        self._row_to_dataset(row),
                        str(row['document_id']),
                        str(row['scene_revision_id']),
                    )
                )
        dataset_a, document_a, revision_a = evidence[0]
        dataset_b, document_b, revision_b = evidence[1]
        if comparison.dataset_a_sha256 != dataset_sha256(dataset_a):
            raise ValueError('comparison dataset A hash mismatch')
        if comparison.dataset_b_sha256 != dataset_sha256(dataset_b):
            raise ValueError('comparison dataset B hash mismatch')
        if (
            comparison.document_id != document_a
            or comparison.document_id != document_b
            or comparison.scene_revision_a_id != revision_a
            or comparison.scene_revision_b_id != revision_b
        ):
            raise ValueError('comparison dataset/revision binding mismatch')
        recomputed = replay_measurement_comparison(
            comparison,
            dataset_a=dataset_a,
            dataset_b=dataset_b,
        )
        if recomputed != comparison.comparison_result():
            raise ValueError(
                'comparison does not match canonical comparison algorithm output'
            )
        return comparison

    def get_comparison(self, comparison_id: str) -> CadMeasurementComparison | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_measurement_comparisons WHERE comparison_id=?',
                (comparison_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate_current_comparison(self._row_to_comparison(row))

    def list_comparisons(self, document_id: str) -> tuple[CadMeasurementComparison, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM cad_measurement_comparisons WHERE document_id=? ORDER BY created_at DESC, comparison_id',
                (document_id,),
            ).fetchall()
        comparisons = tuple(self._row_to_comparison(row) for row in rows)
        for comparison in comparisons:
            self._validate_current_comparison(comparison)
        return comparisons

    @staticmethod
    def _row_to_measurement(row: sqlite3.Row) -> CadMeasurementRecord:
        payload = {
            'measurement_id': row['measurement_id'],
            'document_id': row['document_id'],
            'scene_revision_id': row['scene_revision_id'],
            'scene_content_hash': row['scene_content_hash'],
            'measurement_entity_id': row['measurement_entity_id'],
            'measurement_position': json.loads(row['measurement_position_json']),
            'measurement_direction': None if row['measurement_direction_json'] is None else json.loads(row['measurement_direction_json']),
            'evidence_type': row['evidence_type'],
            'channel_role': row['channel_role'],
            'source_speaker_ids': tuple(json.loads(row['source_speaker_ids_json'])),
            'radiation_scope': row['radiation_scope'],
            'routing_evidence': row['routing_evidence'],
            'captured_at': row['captured_at'],
            'imported_at': row['imported_at'],
            'source_kind': row['source_kind'],
            'external_source_id': row['external_source_id'],
            'quality_status': row['quality_status'],
            'quality_reasons': tuple(json.loads(row['quality_reasons_json'])),
            'quality_source': row['quality_source'],
            'provenance_json': row['provenance_json'],
        }
        return CadMeasurementRecord.model_validate(payload)

    def _asset_row_for_digest(self, digest: str) -> sqlite3.Row:
        with closing(self._connect()) as connection:
            row = connection.execute(
                '''SELECT sha256, filename, relative_path, size_bytes
                   FROM cad_measurement_assets WHERE sha256=?''',
                (digest,),
            ).fetchone()
        if row is None:
            raise ManagedAssetError(
                'measurement raw asset has no cad_measurement_assets row: '
                f'{digest}'
            )
        return row

    def validate_raw_asset(self, digest: str) -> VerifiedMeasurementAsset:
        """Resolve and verify the managed raw asset registered under *digest*.

        This is the authoritative runtime counterpart of the native backup
        ``_validate_asset_contract``: the ``cad_measurement_assets`` row keyed
        by the content-addressed SHA-256 must exist, declare a safe relative
        path contained under this repository's managed assets directory with
        the digest as its leaf name, and resolve to an existing regular file
        (never a symlink) whose stored size and streamed SHA-256 match the
        row. Any gap raises ``ManagedAssetError`` — a typed fail-closed
        error, never a silent pass.
        """
        row = self._asset_row_for_digest(digest)
        sha256_text = str(row['sha256'])
        relative_path = str(row['relative_path'])
        size_bytes = int(row['size_bytes'])
        asset_path = verify_managed_asset(
            data_dir=self.path.parent,
            digest=sha256_text,
            relative_path=relative_path,
            size_bytes=size_bytes,
            required_root=self.assets_dir,
        )
        # The runtime store is content-addressed: the declared path must
        # name the digest as its leaf, so a row pointing at an alias or a
        # foreign file fails even when its bytes happen to verify.
        if asset_path.name != sha256_text:
            raise ManagedAssetError(
                'measurement asset path does not match its content address: '
                f'{relative_path}'
            )
        return VerifiedMeasurementAsset(
            sha256=sha256_text,
            filename=str(row['filename']),
            relative_path=relative_path.replace('\\', '/'),
            size_bytes=size_bytes,
            path=asset_path,
        )

    def validate_raw_asset_for_dataset(self, dataset_id: str) -> VerifiedMeasurementAsset:
        """Resolve the exact verified raw asset backing one persisted dataset.

        Looks up the ``cad_frequency_responses`` row, follows its
        ``source_sha256`` to the managed asset row and enforces the full
        contract via ``validate_raw_asset``. A missing dataset raises
        ``KeyError``; a missing, misplaced or corrupted raw asset raises
        ``ManagedAssetError``.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT source_sha256 FROM cad_frequency_responses WHERE dataset_id=?',
                (dataset_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f'dataset not found: {dataset_id}')
        return self.validate_raw_asset(str(row['source_sha256']))

    def _read_verified_asset(self, asset: VerifiedMeasurementAsset) -> bytes:
        """Read a verified asset's bytes with a TOCTOU content re-check.

        The contract in ``validate_raw_asset`` already streamed the file's
        SHA-256; re-hashing the bytes actually handed to the importer replay
        closes the window where the file changed between verification and
        read.
        """
        try:
            raw = self._asset_store.read_file(asset.path)
        except FileNotFoundError as exc:
            raise ManagedAssetError(
                'measurement raw asset is unavailable for dataset verification'
            ) from exc
        if sha256(raw).hexdigest() != asset.sha256:
            raise ManagedAssetError(
                'measurement raw asset content does not match its content address'
            )
        return raw

    def _verified_raw_asset(self, digest: str) -> bytes:
        """Load the content-addressed raw asset, failing closed on tampering."""
        raw = self._asset_store.read_verified(digest)
        if raw is None:
            raise ValueError(
                'measurement raw asset is unavailable for dataset verification'
            )
        return raw


    def verify_measurement_asset_authority(self, measurement_id: str) -> None:
        """Re-verify the file-backed raw asset bound to *measurement_id*.

        Authoritative production reads must not trust the measurement row
        alone: the bound dataset row is re-attested (semantic hash,
        transformation seal and pinned importer replay over the
        content-addressed raw bytes), then the raw-asset registry entry is
        checked for presence and exact size against the verified bytes. A
        missing or corrupt asset fails closed.
        """
        dataset = self.dataset_for_measurement(measurement_id)
        if dataset is None:
            raise ValueError(
                'measurement has no bound frequency-response dataset: '
                f'{measurement_id}'
            )
        asset = self.validate_raw_asset(dataset.source_sha256)
        raw = self._read_verified_asset(asset)
        if len(raw) != asset.size_bytes:
            raise ManagedAssetError(
                'measurement raw asset size does not match its registry entry'
            )

    def get_evidence_bundle(self, measurement_id: str) -> MeasurementEvidenceBundle:
        """Authoritative raw-backed evidence for one persisted measurement.

        Returns the measurement record, its frequency-response dataset and
        the verified raw asset in one pass. The dataset resolves through the
        same authoritative read path as ``dataset_for_measurement`` —
        persisted seals, the managed raw-asset contract and the pinned
        importer replay — and the returned asset metadata (declared relative
        path, size, verified filesystem path) lets evidence consumers bind
        to the exact raw file without a second verification pass.
        """
        record = self.get_measurement(measurement_id)
        if record is None:
            raise KeyError(measurement_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                f'{self._DATASET_SELECT} WHERE d.measurement_id=?',
                (measurement_id,),
            ).fetchone()
        if row is None:
            raise ValueError(
                'measurement has no frequency-response dataset: '
                f'{measurement_id}'
            )
        dataset, asset = self._dataset_and_asset(row)
        return MeasurementEvidenceBundle(
            measurement=record,
            dataset=dataset,
            raw_asset=asset,
        )

    def _dataset_and_asset(
        self,
        row: sqlite3.Row,
    ) -> tuple[CadFrequencyResponseDataset, VerifiedMeasurementAsset]:
        """Authoritative read: re-verify the persisted import-transformation binding.

        The row must carry the persisted dataset semantic hash and the
        versioned transformation seal; both are recomputed from the stored
        columns, then the exact pinned importer reruns against the
        content-addressed raw asset and must reproduce the row exactly. Rows
        written before the binding existed (NULL columns), rows whose seals
        were recomputed coherently over altered samples, and rows whose raw
        asset was removed or replaced all fail closed.

        The raw asset itself is resolved through ``validate_raw_asset`` —
        the same shared managed asset contract the native backup path
        enforces — so an authoritative read proves the declared
        ``cad_measurement_assets`` row still resolves to a contained regular
        file of the stored size whose content hashes to ``source_sha256``,
        not merely that some file happens to sit at the digest path. This
        per-read verification is deliberate: N60 dataset reads are evidence
        reads, and the repository convention since the import-transformation
        binding is to fail closed on every authoritative read rather than
        cache integrity state. ``get_evidence_bundle`` reuses the verified
        asset produced here so evidence consumers pay for one verification
        pass, not two.
        """
        dataset = CadFrequencyResponseDataset(
            dataset_id=row['dataset_id'],
            measurement_id=row['measurement_id'],
            frequency_hz=_unpack(row['frequency_blob']) or (),
            level_db=_unpack(row['level_blob']) or (),
            phase_deg=_unpack(row['phase_blob']),
            phase_status=row['phase_status'],
            level_reference=row['level_reference'],
            smoothing=row['smoothing'],
            processing_json=row['processing_json'],
            source_sha256=row['source_sha256'],
            importer_version=row['importer_version'],
        )
        stored_dataset_sha256 = row['dataset_sha256']
        stored_transformation_sha256 = row['transformation_sha256']
        if stored_dataset_sha256 is None or stored_transformation_sha256 is None:
            # Backward-compatibility policy: rows written before the
            # import-transformation binding existed never had their samples
            # attested, so they are non-authoritative and fail closed on read
            # rather than acquiring a fabricated current identity.
            raise ValueError(
                'frequency-response dataset predates import-transformation '
                'binding and is non-authoritative'
            )
        if stored_dataset_sha256 != dataset_sha256(dataset):
            raise ValueError('persisted dataset semantic hash mismatch')
        if stored_transformation_sha256 != import_transformation_sha256(
            source_sha256=dataset.source_sha256,
            importer_version=dataset.importer_version,
            dataset_sha256=stored_dataset_sha256,
        ):
            raise ValueError('persisted dataset transformation hash mismatch')
        record_source_kind = (
            row['record_source_kind']
            if 'record_source_kind' in row.keys()
            else None
        )
        asset = self.validate_raw_asset(dataset.source_sha256)
        verify_imported_dataset(
            dataset,
            self._read_verified_asset(asset),
            source_kind=record_source_kind,
        )
        return dataset, asset

    def _row_to_dataset(self, row: sqlite3.Row) -> CadFrequencyResponseDataset:
        return self._dataset_and_asset(row)[0]

    @staticmethod
    def _row_to_comparison(row: sqlite3.Row) -> CadMeasurementComparison:
        result = json.loads(row['result_json'])
        return CadMeasurementComparison.model_validate({
            'comparison_id': row['comparison_id'],
            'document_id': row['document_id'],
            'dataset_a_id': row['dataset_a_id'],
            'dataset_b_id': row['dataset_b_id'],
            'scene_revision_a_id': row['scene_revision_a_id'],
            'scene_revision_b_id': row['scene_revision_b_id'],
            'created_at': row['created_at'],
            **result,
        })


    # Fields that identify the plan's subject; they are fixed for the whole
    # lifecycle of one plan_id. Only status, measurement_ids and the optional
    # prediction-provider binding may change between persisted versions.
    _PLAN_LINEAGE_FIELDS = (
        'document_id',
        'search_spec_id',
        'search_spec_sha256',
        'candidate_id',
        'candidate_set_sha256',
        'applied_scene_revision_id',
        'applied_scene_content_hash',
    )

    @classmethod
    def _plan_chain_violation(cls, head, plan) -> str | None:
        """Return why *plan* cannot extend persisted *head*, or None when valid."""
        if head.status == 'measured':
            return 'the plan is already measured; a measured head is terminal'
        if (
            plan.supersedes_plan_sha256 is not None
            and plan.supersedes_plan_sha256 != head.plan_sha256
        ):
            return (
                f'claims predecessor {plan.supersedes_plan_sha256} '
                f'but the persisted head is {head.plan_sha256}'
            )
        for field in cls._PLAN_LINEAGE_FIELDS:
            if getattr(plan, field) != getattr(head, field):
                return f'changes {field}, which is immutable across the plan lifecycle'
        if plan.status == 'measured' and (
            plan.prediction_provider_binding_id != head.prediction_provider_binding_id
            or plan.prediction_provider_binding_sha256 != head.prediction_provider_binding_sha256
        ):
            return 'measured transition must preserve the head prediction-provider binding'
        return None

    def save_measurement_plan(self, plan) -> None:
        """Append one immutable plan version as the single head of its plan_id lifecycle.

        The persisted history of one ``plan_id`` is an append-only state
        machine enforced under one ``BEGIN IMMEDIATE`` transaction:

        - the first version must be ``planned`` and claim no predecessor;
        - a later ``planned`` version (e.g. a prediction-provider binding
          update) must claim the current head via ``supersedes_plan_sha256``;
        - ``planned -> measured`` is the only completion transition and keeps
          the head's prediction-provider binding;
        - a measured head is terminal.

        Two writers building on the same head cannot both advance it: the head
        check runs inside the write transaction, so the loser sees the moved
        head and fails with ``MeasurementPlanConflictError``. The plan's
        upstream authority (persisted SearchSpec SHA, candidate membership and
        candidate-set SHA, candidate -> applied SceneRevision materialization)
        is revalidated on every save, so the builder remains a convenience and
        not the only integrity boundary.
        """
        from .cad_measurement_loop import CadMeasurementPlan, _resolve_candidate
        from .cad_search import candidate_preview_document
        if not isinstance(plan, CadMeasurementPlan):
            raise TypeError('plan must be CadMeasurementPlan')
        plan = CadMeasurementPlan.model_validate(plan.model_dump(mode='python'))
        spec = self.search_repository.get(plan.search_spec_id)
        if spec is None:
            raise ValueError('measurement plan SearchSpec does not exist')
        if spec.document_id != plan.document_id or spec.search_spec_sha256 != plan.search_spec_sha256:
            raise ValueError('measurement plan SearchSpec authority mismatch')
        source = self.scene_repository.get(spec.scene_revision_id)
        if (
            source is None
            or source.document_id != spec.document_id
            or source.content_hash != spec.scene_content_hash
        ):
            raise ValueError('measurement plan SearchSpec source revision authority is unavailable or changed')
        candidate, candidate_set_sha256 = _resolve_candidate(
            self.scene_repository, spec, plan.candidate_id
        )
        if candidate_set_sha256 != plan.candidate_set_sha256:
            raise ValueError('measurement plan candidate-set hash mismatch')
        revision = self.scene_repository.get(plan.applied_scene_revision_id)
        if revision is None or revision.document_id != plan.document_id or revision.content_hash != plan.applied_scene_content_hash:
            raise ValueError('measurement plan applied revision binding mismatch')
        if revision.parent_revision_id != source.revision_id:
            raise ValueError('measurement plan applied revision must directly descend from the SearchSpec source revision')
        expected = candidate_preview_document(source.document, candidate)
        if scene_content_hash(expected) != revision.content_hash:
            raise ValueError('measurement plan applied revision does not exactly match the selected candidate placement')
        if plan.status == 'measured':
            for measurement_id in plan.measurement_ids:
                record = self.get_measurement(measurement_id)
                if record is None:
                    raise ValueError(f'measurement plan references unknown measurement: {measurement_id}')
                if (
                    record.document_id != plan.document_id
                    or record.scene_revision_id != plan.applied_scene_revision_id
                    or record.scene_content_hash != plan.applied_scene_content_hash
                ):
                    raise ValueError('measurement plan evidence binding mismatch')
                if record.evidence_type != 'measured':
                    raise ValueError('measurement plan may only contain measured evidence')
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            head_row = connection.execute(
                'SELECT plan_sha256, payload_json FROM cad_measurement_plans '
                'WHERE plan_id=? ORDER BY seq DESC LIMIT 1',
                (plan.plan_id,),
            ).fetchone()
            if head_row is None:
                if plan.status != 'planned':
                    raise MeasurementPlanConflictError(
                        f'first measurement plan version for plan_id {plan.plan_id} must be planned'
                    )
                if plan.supersedes_plan_sha256 is not None:
                    raise MeasurementPlanConflictError(
                        f'first measurement plan version for plan_id {plan.plan_id} '
                        'must not claim a predecessor that was never persisted'
                    )
            else:
                head = CadMeasurementPlan.model_validate_json(head_row['payload_json'])
                if head_row['plan_sha256'] != head.plan_sha256:
                    raise ValueError('persisted measurement plan head disagrees with its payload')
                if plan.supersedes_plan_sha256 is None:
                    raise MeasurementPlanConflictError(
                        f'measurement plan {plan.plan_id} already has a persisted head; '
                        'a new version must claim it via supersedes_plan_sha256'
                    )
                violation = self._plan_chain_violation(head, plan)
                if violation is not None:
                    raise MeasurementPlanConflictError(
                        f'measurement plan {plan.plan_id} rejected: {violation}'
                    )
            connection.execute(
                '''INSERT INTO cad_measurement_plans(
                    plan_id, document_id, search_spec_id, candidate_id,
                    applied_scene_revision_id, status, plan_sha256, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (plan.plan_id, plan.document_id, plan.search_spec_id, plan.candidate_id,
                 plan.applied_scene_revision_id, plan.status, plan.plan_sha256, plan.model_dump_json()),
            )
            connection.commit()

    def list_measurement_plans(self, search_spec_id: str):
        """Return the persisted plan history as validated single-head chains.

        Rows are replayed in insertion order per ``plan_id``; each chain must
        start with an unclaimed ``planned`` version and every successor must
        extend its predecessor (explicitly via ``supersedes_plan_sha256``, or
        implicitly for rows persisted before predecessor tracking existed).
        A pre-existing fork, a measured-first root or history continuing past
        a measured terminal is surfaced as ``ValueError`` rather than guessed.
        """
        from .cad_measurement_loop import CadMeasurementPlan
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM cad_measurement_plans WHERE search_spec_id=? ORDER BY seq ASC',
                (search_spec_id,),
            ).fetchall()
        plans = []
        heads: dict[str, CadMeasurementPlan] = {}
        for row in rows:
            plan = CadMeasurementPlan.model_validate_json(row['payload_json'])
            if (
                row['plan_id'] != plan.plan_id
                or row['document_id'] != plan.document_id
                or row['search_spec_id'] != plan.search_spec_id
                or row['candidate_id'] != plan.candidate_id
                or row['applied_scene_revision_id'] != plan.applied_scene_revision_id
                or row['status'] != plan.status
                or row['plan_sha256'] != plan.plan_sha256
            ):
                raise ValueError('persisted measurement plan row disagrees with its payload')
            head = heads.get(plan.plan_id)
            if head is None:
                if plan.status != 'planned':
                    raise ValueError(
                        f'measurement plan history for {plan.plan_id} '
                        'does not start with a planned version'
                    )
                if plan.supersedes_plan_sha256 is not None:
                    raise ValueError(
                        f'measurement plan history for {plan.plan_id} '
                        'starts with a predecessor claim that was never persisted'
                    )
            else:
                violation = self._plan_chain_violation(head, plan)
                if violation is not None:
                    raise ValueError(
                        f'measurement plan history for {plan.plan_id} '
                        f'is not a single chain: {violation}'
                    )
            heads[plan.plan_id] = plan
            plans.append(plan)
        return tuple(plans)

    def latest_measurement_plans(self, search_spec_id: str):
        """Current head of each plan_id's validated single-chain history."""
        history = self.list_measurement_plans(search_spec_id)
        order: list[str] = []
        latest = {}
        for plan in history:
            if plan.plan_id not in latest:
                order.append(plan.plan_id)
            latest[plan.plan_id] = plan
        return tuple(latest[plan_id] for plan_id in order)
