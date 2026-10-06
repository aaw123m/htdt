"""Append-only persistence for REV59-ROOMQ authorities.

Six tables in one repository — sound strength G (#761), resonant
treatment models (#704), serviceability envelope (#707):

* ``cad_sound_strength_observations`` / ``cad_sound_strength_qualifications``
* ``cad_resonant_absorber_profiles`` / ``cad_resonant_performance_records``
* ``cad_service_envelope_profiles`` / ``cad_service_access_observations``
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_sound_strength import (
    SoundStrengthObservation,
    SoundStrengthQualification,
)
from .cad_resonant_treatment import (
    ResonantAbsorberProfile,
    ResonantPerformanceRecord,
)
from .cad_serviceability import (
    ServiceAccessObservation,
    ServiceEnvelopeProfile,
)


class RoomQualificationConflictError(ValueError):
    """A room-qualification save violated append-only identity rules."""


class RoomQualificationIntegrityError(ValueError):
    """A stored room-qualification row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise RoomQualificationIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise RoomQualificationIntegrityError(
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
            raise RoomQualificationConflictError(
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
            raise RoomQualificationIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise RoomQualificationIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise RoomQualificationIntegrityError(
                    f'stored {self.table}.{column} disagrees '
                    'with its payload'
                )
        return record

    def list(self, document_id: str | None = None) -> tuple[Any, ...]:
        query = f'SELECT payload_json FROM {self.table}'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            self.model.model_validate_json(r['payload_json'])
            for r in rows
        )


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadRoomQualificationRepository:
    """Native storage for the #761/#704/#707 authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_sound_strength_observations',
                'cad_sound_strength_qualifications',
                'cad_resonant_absorber_profiles',
                'cad_resonant_performance_records',
                'cad_service_envelope_profiles',
                'cad_service_access_observations',
            )
        self.g_observations = _SealedStore(
            self._connect, 'cad_sound_strength_observations',
            SoundStrengthObservation, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                ('method', 'method'),
                ('band', 'band'),
            ),
        )
        self.g_qualifications = _SealedStore(
            self._connect, 'cad_sound_strength_qualifications',
            SoundStrengthQualification, 'qualification_id',
            'qualification_sha256',
            (
                ('document_id', '__document_id__'),
                ('verdict', 'verdict'),
            ),
        )
        self.resonant_profiles = _SealedStore(
            self._connect, 'cad_resonant_absorber_profiles',
            ResonantAbsorberProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('absorber_kind', 'absorber_kind'),
            ),
        )
        self.resonant_records = _SealedStore(
            self._connect, 'cad_resonant_performance_records',
            ResonantPerformanceRecord, 'record_id', 'record_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
                ('derivation', 'derivation'),
            ),
        )
        self.service_envelopes = _SealedStore(
            self._connect, 'cad_service_envelope_profiles',
            ServiceEnvelopeProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
            ),
        )
        self.service_observations = _SealedStore(
            self._connect, 'cad_service_access_observations',
            ServiceAccessObservation, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('envelope_ref_id', 'envelope_ref'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_g_observation(
        self, record: SoundStrengthObservation
    ) -> None:
        self.g_observations.save(record)

    def get_g_observation(
        self, rid: str
    ) -> SoundStrengthObservation | None:
        return self.g_observations.get(rid)

    def save_g_qualification(
        self, record: SoundStrengthQualification
    ) -> None:
        self.g_qualifications.save(record)

    def get_g_qualification(
        self, rid: str
    ) -> SoundStrengthQualification | None:
        return self.g_qualifications.get(rid)

    def save_resonant_profile(
        self, record: ResonantAbsorberProfile
    ) -> None:
        self.resonant_profiles.save(record)

    def get_resonant_profile(
        self, rid: str
    ) -> ResonantAbsorberProfile | None:
        return self.resonant_profiles.get(rid)

    def save_resonant_record(
        self, record: ResonantPerformanceRecord
    ) -> None:
        self.resonant_records.save(record)

    def get_resonant_record(
        self, rid: str
    ) -> ResonantPerformanceRecord | None:
        return self.resonant_records.get(rid)

    def save_service_envelope(
        self, record: ServiceEnvelopeProfile
    ) -> None:
        self.service_envelopes.save(record)

    def get_service_envelope(
        self, rid: str
    ) -> ServiceEnvelopeProfile | None:
        return self.service_envelopes.get(rid)

    def save_service_observation(
        self, record: ServiceAccessObservation
    ) -> None:
        self.service_observations.save(record)

    def get_service_observation(
        self, rid: str
    ) -> ServiceAccessObservation | None:
        return self.service_observations.get(rid)
