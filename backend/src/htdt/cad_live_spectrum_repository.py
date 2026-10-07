"""Append-only persistence for the #793 live-spectrum authority.

Five tables in one repository — real-time measurement sessions, live
spectrum observations, SPL time histories, captured live traces and
live event annotations:

* ``cad_realtime_measurement_sessions``
* ``cad_live_spectrum_observations``
* ``cad_spl_time_histories``
* ``cad_captured_live_traces``
* ``cad_live_event_annotations``
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_live_spectrum import (
    CapturedLiveTrace,
    LiveEventAnnotation,
    LiveSpectrumObservation,
    RealtimeMeasurementSession,
    SPLTimeHistory,
)


class LiveSpectrumConflictError(ValueError):
    """A live-spectrum save violated append-only identity rules."""


class LiveSpectrumIntegrityError(ValueError):
    """A stored live-spectrum row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise LiveSpectrumIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise LiveSpectrumIntegrityError(
            'record id does not match its sealed sha256'
        )


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

    def save(self, record: Any) -> None:
        _assert_sealed(record, self.sha_field, self.id_field)
        rid = getattr(record, self.id_field)
        existing = self.get(rid)
        if existing is not None:
            if getattr(existing, self.sha_field) == getattr(
                record, self.sha_field
            ):
                return
            raise LiveSpectrumConflictError(
                f'{self.table} records are append-only'
            )
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
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f'INSERT INTO {self.table} ({cols}) '
                f'VALUES ({placeholders})',
                values,
            )

    def get(self, rid: str) -> Any | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                f'SELECT * FROM {self.table} WHERE {self.id_field}=?',
                (rid,),
            ).fetchone()
        if row is None:
            return None
        record = self.model.model_validate_json(row['payload_json'])
        if getattr(record, self.id_field) != row[self.id_field]:
            raise LiveSpectrumIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise LiveSpectrumIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise LiveSpectrumIntegrityError(
                    f'stored {self.table}.{column} disagrees '
                    'with its payload'
                )
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
                raise LiveSpectrumIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload'
                )
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadLiveSpectrumRepository:
    """Native storage for the #793 live-observation authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_realtime_measurement_sessions',
                'cad_live_spectrum_observations',
                'cad_spl_time_histories',
                'cad_captured_live_traces',
                'cad_live_event_annotations',
            )
        self.sessions = _SealedStore(
            self._connect, 'cad_realtime_measurement_sessions',
            RealtimeMeasurementSession, 'session_id', 'session_sha256',
            (
                ('document_id', '__document_id__'),
                ('input_channel', 'input_channel'),
                ('input_domain', 'input_domain'),
            ),
        )
        self.observations = _SealedStore(
            self._connect, 'cad_live_spectrum_observations',
            LiveSpectrumObservation, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('session_ref_id', 'session_ref'),
                ('mode', 'mode'),
                ('capture_state', 'capture_state'),
            ),
        )
        self.histories = _SealedStore(
            self._connect, 'cad_spl_time_histories',
            SPLTimeHistory, 'history_id', 'history_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('session_ref_id', 'session_ref'),
                ('quantity', 'quantity'),
            ),
        )
        self.traces = _SealedStore(
            self._connect, 'cad_captured_live_traces',
            CapturedLiveTrace, 'trace_id', 'trace_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('session_ref_id', 'session_ref'),
                ('capture_kind', 'capture_kind'),
            ),
        )
        self.annotations = _SealedStore(
            self._connect, 'cad_live_event_annotations',
            LiveEventAnnotation, 'annotation_id', 'annotation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('session_ref_id', 'session_ref'),
                ('source', 'source'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # sessions
    def save_session(self, record: RealtimeMeasurementSession) -> None:
        self.sessions.save(record)

    def get_session(self, rid: str) -> RealtimeMeasurementSession | None:
        return self.sessions.get(rid)

    def list_sessions(
        self, document_id: str | None = None
    ) -> tuple[RealtimeMeasurementSession, ...]:
        return self.sessions.list(document_id)

    # observations
    def save_observation(self, record: LiveSpectrumObservation) -> None:
        self.observations.save(record)

    def get_observation(self, rid: str) -> LiveSpectrumObservation | None:
        return self.observations.get(rid)

    def list_observations(
        self, document_id: str | None = None
    ) -> tuple[LiveSpectrumObservation, ...]:
        return self.observations.list(document_id)

    # histories
    def save_history(self, record: SPLTimeHistory) -> None:
        self.histories.save(record)

    def get_history(self, rid: str) -> SPLTimeHistory | None:
        return self.histories.get(rid)

    def list_histories(
        self, document_id: str | None = None
    ) -> tuple[SPLTimeHistory, ...]:
        return self.histories.list(document_id)

    # traces
    def save_trace(self, record: CapturedLiveTrace) -> None:
        self.traces.save(record)

    def get_trace(self, rid: str) -> CapturedLiveTrace | None:
        return self.traces.get(rid)

    def list_traces(
        self, document_id: str | None = None
    ) -> tuple[CapturedLiveTrace, ...]:
        return self.traces.list(document_id)

    # annotations
    def save_annotation(self, record: LiveEventAnnotation) -> None:
        self.annotations.save(record)

    def get_annotation(self, rid: str) -> LiveEventAnnotation | None:
        return self.annotations.get(rid)

    def list_annotations(
        self, document_id: str | None = None
    ) -> tuple[LiveEventAnnotation, ...]:
        return self.annotations.list(document_id)
