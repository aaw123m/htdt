from __future__ import annotations

from contextlib import closing
from array import array
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from uuid import uuid4

from .cad_measurement_models import CadFrequencyResponseDataset, CadMeasurementComparison, CadMeasurementRecord
from .cad_repository import SceneRepository, SceneRevision
from .cad_scene import acoustic_reference_position
from .comparison import ComparisonResult


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
        self.path = scene_repository.path
        self.assets_dir = Path(assets_dir) if assets_dir is not None else self.path.parent / 'measurement-assets'
        self.assets_dir.mkdir(parents=True, exist_ok=True)
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
                    importer_version TEXT NOT NULL
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
        return self.assets_dir / digest

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        # Making the rename durable requires a directory fsync, which is
        # only meaningful on POSIX filesystems.
        if os.name != 'posix':
            return
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _read_asset(target: Path, attempts: int = 100) -> bytes:
        # A file being atomically replaced may briefly refuse reads on
        # Windows while the previous handle is pending deletion.
        for attempt in range(attempts):
            try:
                return target.read_bytes()
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.01)
        raise RuntimeError('unreachable')

    def _install_asset(self, digest: str, raw_bytes: bytes) -> None:
        """Durably install *raw_bytes* at the content-addressed digest path.

        The payload is written to a unique temporary file in the same
        filesystem, flushed, fsynced and verified before being atomically
        renamed onto the digest path, so the final path never exposes a
        partially written file. Only the private temporary file is removed
        on failure; an already-installed digest path is never touched.
        """
        target = self._asset_path(digest)
        descriptor, temp_name = tempfile.mkstemp(
            dir=self.assets_dir, prefix='.asset-', suffix='.tmp'
        )
        temp = Path(temp_name)
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(raw_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            persisted = temp.read_bytes()
            if len(persisted) != len(raw_bytes) or sha256(persisted).hexdigest() != digest:
                raise RuntimeError('measurement asset write verification failed')
            try:
                os.replace(temp, target)
            except PermissionError:
                # Windows can refuse a replace while a racing install holds
                # the destination; an identical already-installed digest is a
                # success, anything else means the install genuinely failed.
                try:
                    installed = self._read_asset(target) == raw_bytes
                except OSError:
                    installed = False
                if not installed:
                    raise
            self._fsync_directory(self.assets_dir)
        finally:
            temp.unlink(missing_ok=True)

    def save(
        self,
        record: CadMeasurementRecord,
        dataset: CadFrequencyResponseDataset,
        *,
        raw_filename: str,
        raw_bytes: bytes,
    ) -> None:
        self._validated_revision(record)
        if dataset.measurement_id != record.measurement_id:
            raise ValueError('dataset measurement_id does not match measurement record')
        digest = sha256(raw_bytes).hexdigest()
        if digest != dataset.source_sha256:
            raise ValueError('raw asset SHA-256 does not match dataset source_sha256')
        target = self._asset_path(digest)
        if target.exists():
            # An already-installed identical digest is a successful dedup hit;
            # anything else at the path is corrupt or a genuine collision.
            if self._read_asset(target) != raw_bytes:
                raise ValueError('content-addressed measurement asset hash collision')
        else:
            self._install_asset(digest, raw_bytes)

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
                    source_sha256, importer_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
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

    def get_dataset(self, dataset_id: str) -> CadFrequencyResponseDataset | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_frequency_responses WHERE dataset_id=?',
                (dataset_id,),
            ).fetchone()
        return None if row is None else self._row_to_dataset(row)

    def dataset_for_measurement(self, measurement_id: str) -> CadFrequencyResponseDataset | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_frequency_responses WHERE measurement_id=?',
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

    def save_comparison(
        self,
        dataset_a_id: str,
        dataset_b_id: str,
        result: ComparisonResult,
    ) -> CadMeasurementComparison:
        if dataset_a_id == dataset_b_id:
            raise ValueError('comparison requires two different datasets')
        with closing(self._connect()) as connection, connection:
            rows = []
            for dataset_id in (dataset_a_id, dataset_b_id):
                row = connection.execute(
                    '''SELECT d.dataset_id, m.document_id, m.scene_revision_id
                       FROM cad_frequency_responses d
                       JOIN cad_measurements m ON m.measurement_id=d.measurement_id
                       WHERE d.dataset_id=?''',
                    (dataset_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f'dataset not found: {dataset_id}')
                rows.append(row)
            if rows[0]['document_id'] != rows[1]['document_id']:
                raise ValueError('comparison datasets belong to different documents')
            comparison = CadMeasurementComparison(
                comparison_id=str(uuid4()),
                document_id=rows[0]['document_id'],
                dataset_a_id=dataset_a_id,
                dataset_b_id=dataset_b_id,
                scene_revision_a_id=rows[0]['scene_revision_id'],
                scene_revision_b_id=rows[1]['scene_revision_id'],
                created_at=_utc_now(),
                **asdict(result),
            )
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
                    json.dumps(asdict(result), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False),
                ),
            )
            connection.commit()
        return comparison

    def get_comparison(self, comparison_id: str) -> CadMeasurementComparison | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_measurement_comparisons WHERE comparison_id=?',
                (comparison_id,),
            ).fetchone()
        return None if row is None else self._row_to_comparison(row)

    def list_comparisons(self, document_id: str) -> tuple[CadMeasurementComparison, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM cad_measurement_comparisons WHERE document_id=? ORDER BY created_at DESC, comparison_id',
                (document_id,),
            ).fetchall()
        return tuple(self._row_to_comparison(row) for row in rows)

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

    @staticmethod
    def _row_to_dataset(row: sqlite3.Row) -> CadFrequencyResponseDataset:
        return CadFrequencyResponseDataset(
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


    def save_measurement_plan(self, plan) -> None:
        from .cad_measurement_loop import CadMeasurementPlan
        if not isinstance(plan, CadMeasurementPlan):
            raise TypeError('plan must be CadMeasurementPlan')
        plan = CadMeasurementPlan.model_validate(plan.model_dump(mode='python'))
        revision = self.scene_repository.get(plan.applied_scene_revision_id)
        if revision is None or revision.document_id != plan.document_id or revision.content_hash != plan.applied_scene_content_hash:
            raise ValueError('measurement plan applied revision binding mismatch')
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
            connection.execute(
                '''INSERT INTO cad_measurement_plans(
                    plan_id, document_id, search_spec_id, candidate_id,
                    applied_scene_revision_id, status, plan_sha256, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (plan.plan_id, plan.document_id, plan.search_spec_id, plan.candidate_id,
                 plan.applied_scene_revision_id, plan.status, plan.plan_sha256, plan.model_dump_json()),
            )

    def list_measurement_plans(self, search_spec_id: str):
        from .cad_measurement_loop import CadMeasurementPlan
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_measurement_plans WHERE search_spec_id=? ORDER BY seq ASC',
                (search_spec_id,),
            ).fetchall()
        return tuple(CadMeasurementPlan.model_validate_json(row['payload_json']) for row in rows)

    def latest_measurement_plans(self, search_spec_id: str):
        history = self.list_measurement_plans(search_spec_id)
        order: list[str] = []
        latest = {}
        for plan in history:
            if plan.plan_id not in latest:
                order.append(plan.plan_id)
            latest[plan.plan_id] = plan
        return tuple(latest[plan_id] for plan_id in order)
