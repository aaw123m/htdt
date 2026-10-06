"""Append-only persistence for the #790 playback-electronics authority.

Four tables in one repository — electronic audio-path profiles,
electrical transfer measurements, electronic linearity evidence and
playback-electronics qualifications.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_playback_electronics import (
    ElectricalTransferMeasurement,
    ElectronicAudioPathProfile,
    ElectronicLinearityEvidence,
    PlaybackElectronicsQualification,
)


class PlaybackElectronicsConflictError(ValueError):
    """A playback-electronics save violated append-only identity rules."""


class PlaybackElectronicsIntegrityError(ValueError):
    """A stored playback-electronics row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise PlaybackElectronicsIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise PlaybackElectronicsIntegrityError(
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
            raise PlaybackElectronicsConflictError(
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
            raise PlaybackElectronicsIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise PlaybackElectronicsIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise PlaybackElectronicsIntegrityError(
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
                raise PlaybackElectronicsIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload'
                )
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadPlaybackElectronicsRepository:
    """Native storage for the #790 playback-electronics authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_electronic_audio_path_profiles',
                'cad_electrical_transfer_measurements',
                'cad_electronic_linearity_evidence',
                'cad_playback_electronics_qualifications',
            )

        self.path_profiles = _SealedStore(
            self._connect, 'cad_electronic_audio_path_profiles',
            ElectronicAudioPathProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('path_class', 'path_class'),
            ),
        )

        self.transfer_measurements = _SealedStore(
            self._connect, 'cad_electrical_transfer_measurements',
            ElectricalTransferMeasurement, 'measurement_id',
            'measurement_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
                ('channel_label', 'channel_label'),
                ('measurement_class', 'measurement_class'),
                ('deembedding_state', 'deembedding_state'),
            ),
        )

        self.linearity_evidence = _SealedStore(
            self._connect, 'cad_electronic_linearity_evidence',
            ElectronicLinearityEvidence, 'evidence_id', 'evidence_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
                ('domain', 'domain'),
                ('observed_regime', 'observed_regime'),
            ),
        )

        self.qualifications = _SealedStore(
            self._connect, 'cad_playback_electronics_qualifications',
            PlaybackElectronicsQualification, 'qualification_id',
            'qualification_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
                ('qualification_state', 'qualification_state'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_path_profile(self, record: ElectronicAudioPathProfile) -> None:
        self.path_profiles.save(record)

    def get_path_profile(
        self, rid: str
    ) -> ElectronicAudioPathProfile | None:
        return self.path_profiles.get(rid)

    def list_path_profiles(
        self, document_id: str | None = None
    ) -> tuple[ElectronicAudioPathProfile, ...]:
        return self.path_profiles.list(document_id)

    def save_transfer_measurement(
        self, record: ElectricalTransferMeasurement
    ) -> None:
        self.transfer_measurements.save(record)

    def get_transfer_measurement(
        self, rid: str
    ) -> ElectricalTransferMeasurement | None:
        return self.transfer_measurements.get(rid)

    def list_transfer_measurements(
        self, document_id: str | None = None
    ) -> tuple[ElectricalTransferMeasurement, ...]:
        return self.transfer_measurements.list(document_id)

    def save_linearity_evidence(
        self, record: ElectronicLinearityEvidence
    ) -> None:
        self.linearity_evidence.save(record)

    def get_linearity_evidence(
        self, rid: str
    ) -> ElectronicLinearityEvidence | None:
        return self.linearity_evidence.get(rid)

    def list_linearity_evidence(
        self, document_id: str | None = None
    ) -> tuple[ElectronicLinearityEvidence, ...]:
        return self.linearity_evidence.list(document_id)

    def save_qualification(
        self, record: PlaybackElectronicsQualification
    ) -> None:
        self.qualifications.save(record)

    def get_qualification(
        self, rid: str
    ) -> PlaybackElectronicsQualification | None:
        return self.qualifications.get(rid)

    def list_qualifications(
        self, document_id: str | None = None
    ) -> tuple[PlaybackElectronicsQualification, ...]:
        return self.qualifications.list(document_id)
