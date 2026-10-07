"""Append-only persistence for the #869 sweep-acquisition authority.

Three sealed stores in one repository — stimulus definitions
(``cad_sweep_stimulus_definitions``), acquisition runs
(``cad_sweep_acquisition_runs``) and stage events
(``cad_sweep_acquisition_stage_events``) — plus content-addressed
managed-asset installs for the raw recording and derived IR, recorded in
the shared ``cad_measurement_assets`` table like every other retained
measurement byte.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from hashlib import sha256
from pathlib import Path
from typing import Any

import numpy as np

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .cad_authority_resolver import AuthorityRef
from .managed_assets import MANAGED_ASSETS_DIRNAME, ManagedAssetStore
from .cad_sweep_acquisition import (
    AcquisitionResult,
    MeasurementAcquisitionEngine,
)
from .cad_sweep_acquisition_evidence import (
    CadSweepAcquisitionRun,
    CadSweepAcquisitionStageEvent,
    CadSweepStimulusDefinition,
    build_acquisition_run,
    build_stage_event,
)


class SweepAcquisitionConflictError(ValueError):
    """A sweep-acquisition save violated append-only identity rules."""


class SweepAcquisitionIntegrityError(ValueError):
    """A stored sweep-acquisition row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    from .canonical_json import canonical_sha256

    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise SweepAcquisitionIntegrityError(
            'record payload does not match its sealed sha256')
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise SweepAcquisitionIntegrityError(
            'record id does not match its sealed sha256')


class _SealedStore:
    """Generic append-only store for one sealed record type."""

    def __init__(
        self,
        connection_factory: Any,
        table: str,
        model: type,
        id_field: str,
        sha_field: str,
        columns: tuple[tuple[str, str], ...],
    ) -> None:
        self._connect = connection_factory
        self.table = table
        self.model = model
        self.id_field = id_field
        self.sha_field = sha_field
        self.columns = columns

    def _column_value(self, record: Any, path: str) -> Any:
        if path == '__document_id__':
            return record.document_id
        value: Any = record
        for part in path.split('.'):
            value = getattr(value, part)
            if value is None:
                return None
        return value

    def save(self, record: Any, connection: Any | None = None) -> None:
        if connection is not None:
            self._save_in(connection, record)
            return
        with closing(self._connect()) as owned, owned:
            self._save_in(owned, record)

    def _save_in(self, connection: Any, record: Any) -> None:
        _assert_sealed(record, self.sha_field, self.id_field)
        rid = getattr(record, self.id_field)
        existing = self._get_in(connection, rid)
        if existing is not None:
            if getattr(existing, self.sha_field) == getattr(
                    record, self.sha_field):
                return
            raise SweepAcquisitionConflictError(
                f'{self.table} records are append-only')
        cols = ', '.join(
            [self.id_field, self.sha_field]
            + [c[0] for c in self.columns]
            + ['payload_json']
        )
        placeholders = ', '.join(['?'] * (2 + len(self.columns) + 1))
        values = (
            rid,
            getattr(record, self.sha_field),
            *(
                self._column_value(record, path)
                for _, path in self.columns
            ),
            record.model_dump_json(),
        )
        connection.execute(
            f'INSERT INTO {self.table} ({cols}) '
            f'VALUES ({placeholders})',
            values,
        )

    def get(self, rid: str) -> Any | None:
        with closing(self._connect()) as connection:
            return self._get_in(connection, rid)

    def _get_in(self, connection: Any, rid: str) -> Any | None:
        row = connection.execute(
            f'SELECT * FROM {self.table} WHERE {self.id_field}=?',
            (rid,),
        ).fetchone()
        if row is None:
            return None
        record = self.model.model_validate_json(row['payload_json'])
        if getattr(record, self.id_field) != row[self.id_field]:
            raise SweepAcquisitionIntegrityError(
                f'stored {self.table} id disagrees with its payload')
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise SweepAcquisitionIntegrityError(
                f'stored {self.table} sha disagrees with its payload')
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise SweepAcquisitionIntegrityError(
                    f'stored {self.table}.{column} disagrees '
                    'with its payload')
        return record

    def list(self, document_id: str | None = None) -> tuple[Any, ...]:
        query = f'SELECT * FROM {self.table}'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        records = []
        for row in rows:
            record = self.model.model_validate_json(row['payload_json'])
            if record.document_id != row['document_id']:
                raise SweepAcquisitionIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload')
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


def _pcm_bytes(samples: np.ndarray) -> bytes:
    """Canonical float64-LE PCM bytes for a retained audio artifact."""

    return np.ascontiguousarray(np.asarray(samples, dtype=np.float64),
                                dtype='<f8').tobytes()


class CadSweepAcquisitionRepository:
    """Native storage for the #869 sweep-acquisition authority."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        assets_dir: Path | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self.assets_dir = (
            Path(assets_dir) if assets_dir is not None
            else self.path.parent / MANAGED_ASSETS_DIRNAME
        )
        self._asset_store = ManagedAssetStore(self.assets_dir)
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_sweep_stimulus_definitions',
                'cad_sweep_acquisition_runs',
                'cad_sweep_acquisition_stage_events',
                'cad_measurement_assets',
            )
        self.stimulus_definitions = _SealedStore(
            self._connect, 'cad_sweep_stimulus_definitions',
            CadSweepStimulusDefinition, 'stimulus_definition_id',
            'stimulus_sha256',
            (
                ('document_id', '__document_id__'),
                ('generator_version', 'generator_version'),
                ('sample_rate_hz', 'sample_rate_hz'),
                ('start_frequency_hz', 'start_frequency_hz'),
                ('end_frequency_hz', 'end_frequency_hz'),
                ('duration_s', 'duration_s'),
                ('level_dbfs', 'level_dbfs'),
                ('repetitions', 'repetitions'),
                ('params_sha256', 'params_sha256'),
                ('samples_sha256', 'samples_sha256'),
                ('created_at_utc', 'created_at_utc'),
            ),
        )
        self.acquisition_runs = _SealedStore(
            self._connect, 'cad_sweep_acquisition_runs',
            CadSweepAcquisitionRun, 'acquisition_id',
            'acquisition_sha256',
            (
                ('document_id', '__document_id__'),
                ('run_id', 'run_id'),
                ('engine_version', 'engine_version'),
                ('backend_id', 'backend_id'),
                ('backend_version', 'backend_version'),
                ('backend_is_simulated', 'backend_is_simulated'),
                _ref('stimulus_ref_id', 'stimulus_ref'),
                ('playback_device_id', 'playback_device_id'),
                ('capture_device_id', 'capture_device_id'),
                ('requested_sample_rate_hz', 'requested_sample_rate_hz'),
                ('actual_sample_rate_hz', 'actual_sample_rate_hz'),
                ('timing_method', 'timing_method'),
                ('timing_quality', 'timing_quality'),
                ('raw_audio_sha256', 'raw_audio_sha256'),
                ('ir_sha256', 'ir_sha256'),
                ('calibration_state', 'calibration_state'),
                ('stage', 'stage'),
                ('outcome', 'outcome'),
                ('quality_verdict', 'quality_verdict'),
                ('captured_at_utc', 'captured_at_utc'),
                ('completed_at_utc', 'completed_at_utc'),
            ),
        )
        self.stage_events = _SealedStore(
            self._connect, 'cad_sweep_acquisition_stage_events',
            CadSweepAcquisitionStageEvent, 'event_id',
            'event_sha256',
            (
                ('document_id', '__document_id__'),
                ('run_id', 'run_id'),
                ('run_seq', 'run_seq'),
                ('stage', 'stage'),
                ('entered_at_utc', 'entered_at_utc'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # stimulus definitions -------------------------------------------

    def save_stimulus_definition(
        self, record: CadSweepStimulusDefinition,
    ) -> None:
        self.stimulus_definitions.save(record)

    def get_stimulus_definition(
        self, stimulus_definition_id: str,
    ) -> CadSweepStimulusDefinition | None:
        return self.stimulus_definitions.get(stimulus_definition_id)

    def list_stimulus_definitions(
        self, document_id: str | None = None,
    ) -> tuple[CadSweepStimulusDefinition, ...]:
        return self.stimulus_definitions.list(document_id)

    # acquisition runs ------------------------------------------------

    def save_acquisition_run(self, record: CadSweepAcquisitionRun) -> None:
        self.acquisition_runs.save(record)

    def get_acquisition_run(
        self, acquisition_id: str,
    ) -> CadSweepAcquisitionRun | None:
        return self.acquisition_runs.get(acquisition_id)

    def get_run_by_run_id(
        self, run_id: str,
    ) -> CadSweepAcquisitionRun | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT acquisition_id FROM cad_sweep_acquisition_runs '
                'WHERE run_id=?', (run_id,),
            ).fetchone()
        if row is None:
            return None
        return self.get_acquisition_run(row['acquisition_id'])

    def list_acquisition_runs(
        self, document_id: str | None = None,
    ) -> tuple[CadSweepAcquisitionRun, ...]:
        return self.acquisition_runs.list(document_id)

    # stage events -----------------------------------------------------

    def save_stage_event(self, record: CadSweepAcquisitionStageEvent) -> None:
        self.stage_events.save(record)

    def get_stage_event(
        self, event_id: str,
    ) -> CadSweepAcquisitionStageEvent | None:
        return self.stage_events.get(event_id)

    def list_stage_events(
        self, run_id: str,
    ) -> tuple[CadSweepAcquisitionStageEvent, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT * FROM cad_sweep_acquisition_stage_events '
                'WHERE run_id=? ORDER BY run_seq ASC', (run_id,),
            ).fetchall()
        events = []
        for row in rows:
            record = CadSweepAcquisitionStageEvent.model_validate_json(
                row['payload_json'])
            for column, path in self.stage_events.columns:
                expected = self.stage_events._column_value(record, path)
                if isinstance(expected, bool):
                    expected = int(expected)
                if row[column] != expected:
                    raise SweepAcquisitionIntegrityError(
                        'stored stage event column disagrees with payload')
            events.append(record)
        return tuple(events)

    # high-level: record a finished engine run --------------------------

    def record_run(
        self,
        *,
        document_id: str,
        engine: MeasurementAcquisitionEngine,
        result: AcquisitionResult,
        stimulus_ref: AuthorityRef,
        calibration_ref: AuthorityRef | None = None,
        project_ref: AuthorityRef | None = None,
        scene_ref: AuthorityRef | None = None,
        campaign_ref: AuthorityRef | None = None,
        role_refs: tuple[AuthorityRef, ...] = (),
        position_ref: AuthorityRef | None = None,
        orientation_ref: AuthorityRef | None = None,
        notes: tuple[str, ...] = (),
    ) -> CadSweepAcquisitionRun:
        """Persist retained evidence for one terminal run.

        Raw recording and derived IR bytes are installed into the managed
        asset store (content-addressed; dedup is a success) and indexed in
        ``cad_measurement_assets`` inside the same write transaction as
        the sealed rows, so a referenced asset can never be missing.
        """

        raw_sha: str | None = None
        raw_path: str | None = None
        raw_bytes: bytes | None = None
        if result.capture is not None and result.capture.recorded_frames:
            raw_bytes = _pcm_bytes(np.asarray(result.capture.samples))
            raw_sha = sha256(raw_bytes).hexdigest()
        ir_sha: str | None = None
        ir_path: str | None = None
        ir_bytes: bytes | None = None
        ir_derivation_sha: str | None = None
        if (result.impulse_response is not None
                and result.impulse_response.ir is not None):
            ir_bytes = _pcm_bytes(np.asarray(result.impulse_response.ir))
            ir_sha = sha256(ir_bytes).hexdigest()
            # The deriver's semantic identity hash pins the derivation;
            # the raw-bytes sha256 is the managed-asset content address.
            ir_derivation_sha = result.impulse_response.ir_sha256

        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if raw_bytes is not None and raw_sha is not None:
                self._asset_store.ensure_installed(raw_sha, raw_bytes)
                raw_path = self._asset_store.asset_path(
                    raw_sha).relative_to(self.path.parent).as_posix()
                connection.execute(
                    '''INSERT OR IGNORE INTO cad_measurement_assets(
                        sha256, filename, relative_path, size_bytes
                    ) VALUES (?, ?, ?, ?)''',
                    (raw_sha, f'{engine.run_id}-raw.f64le', raw_path,
                     len(raw_bytes)),
                )
            if ir_bytes is not None and ir_sha is not None:
                self._asset_store.ensure_installed(ir_sha, ir_bytes)
                ir_path = self._asset_store.asset_path(
                    ir_sha).relative_to(self.path.parent).as_posix()
                connection.execute(
                    '''INSERT OR IGNORE INTO cad_measurement_assets(
                        sha256, filename, relative_path, size_bytes
                    ) VALUES (?, ?, ?, ?)''',
                    (ir_sha, f'{engine.run_id}-ir.f64le', ir_path,
                     len(ir_bytes)),
                )
            # Build the sealed record with the resolved asset paths bound in.
            record = build_acquisition_run(
                document_id=document_id,
                engine=engine, result=result,
                stimulus_ref=stimulus_ref,
                calibration_ref=calibration_ref,
                project_ref=project_ref, scene_ref=scene_ref,
                campaign_ref=campaign_ref, role_refs=role_refs,
                position_ref=position_ref,
                orientation_ref=orientation_ref,
                raw_audio_sha256=raw_sha,
                raw_audio_asset_path=raw_path,
                ir_sha256=ir_sha,
                ir_derivation_sha256=ir_derivation_sha,
                ir_asset_path=ir_path,
                notes=notes,
            )
            # Sealed stores validate + insert inside this transaction.
            self.acquisition_runs.save(record, connection=connection)
            for seq, transition in enumerate(engine.transitions):
                self.stage_events.save(build_stage_event(
                    document_id=document_id,
                    run_id=engine.run_id,
                    run_seq=seq,
                    stage=transition.stage,
                    reason=transition.reason,
                    entered_at_utc=transition.at_utc,
                ), connection=connection)
        return record

    # asset verification ------------------------------------------------

    def read_raw_audio(self, record: CadSweepAcquisitionRun) -> bytes | None:
        if record.raw_audio_sha256 is None:
            return None
        return self._asset_store.read_verified(record.raw_audio_sha256)

    def read_ir(self, record: CadSweepAcquisitionRun) -> bytes | None:
        if record.ir_sha256 is None:
            return None
        return self._asset_store.read_verified(record.ir_sha256)


__all__ = [
    'CadSweepAcquisitionRepository', 'SweepAcquisitionConflictError',
    'SweepAcquisitionIntegrityError',
]
