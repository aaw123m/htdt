"""Append-only persistence for REV59-POWEREV authorities.

Seven tables in one repository — power sequencing (#736), AC power
quality (#738), EMC product evidence (#752):

* ``cad_power_sequencing_profiles`` / ``cad_power_sequence_events`` /
  ``cad_ups_transition_records``
* ``cad_power_quality_measurements`` / ``cad_power_quality_qualifications``
* ``cad_emc_product_profiles`` / ``cad_emc_symptom_records``
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_power_sequencing import (
    PowerSequenceEvent,
    PowerSequencingProfile,
    UpsTransitionRecord,
)
from .cad_power_quality import (
    PowerQualityMeasurement,
    PowerQualityQualification,
)
from .cad_emc_evidence import (
    EmcProductProfile,
    EmcSymptomRecord,
)


class PowerEvidenceConflictError(ValueError):
    """A power-evidence save violated append-only identity rules."""


class PowerEvidenceIntegrityError(ValueError):
    """A stored power-evidence row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise PowerEvidenceIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise PowerEvidenceIntegrityError(
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
            raise PowerEvidenceConflictError(
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
            raise PowerEvidenceIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise PowerEvidenceIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise PowerEvidenceIntegrityError(
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
                raise PowerEvidenceIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload'
                )
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadPowerEvidenceRepository:
    """Native storage for the #736/#738/#752 authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_power_sequencing_profiles',
                'cad_power_sequence_events',
                'cad_ups_transition_records',
                'cad_power_quality_measurements',
                'cad_power_quality_qualifications',
                'cad_emc_product_profiles',
                'cad_emc_symptom_records',
            )
        self.sequencing_profiles = _SealedStore(
            self._connect, 'cad_power_sequencing_profiles',
            PowerSequencingProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('direction', 'direction'),
            ),
        )
        self.sequence_events = _SealedStore(
            self._connect, 'cad_power_sequence_events',
            PowerSequenceEvent, 'event_id', 'event_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
            ),
        )
        self.ups_transitions = _SealedStore(
            self._connect, 'cad_ups_transition_records',
            UpsTransitionRecord, 'record_id', 'record_sha256',
            (
                ('document_id', '__document_id__'),
                ('ups_device_id', 'ups_device_id'),
                ('transfer_observed', 'transfer_observed'),
            ),
        )
        self.pq_measurements = _SealedStore(
            self._connect, 'cad_power_quality_measurements',
            PowerQualityMeasurement, 'measurement_id',
            'measurement_sha256',
            (
                ('document_id', '__document_id__'),
                ('circuit_id', 'circuit_id'),
            ),
        )
        self.pq_qualifications = _SealedStore(
            self._connect, 'cad_power_quality_qualifications',
            PowerQualityQualification, 'qualification_id',
            'qualification_sha256',
            (
                ('document_id', '__document_id__'),
                ('circuit_id', 'circuit_id'),
                ('verdict', 'verdict'),
            ),
        )
        self.emc_profiles = _SealedStore(
            self._connect, 'cad_emc_product_profiles',
            EmcProductProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
            ),
        )
        self.emc_symptoms = _SealedStore(
            self._connect, 'cad_emc_symptom_records',
            EmcSymptomRecord, 'record_id', 'record_sha256',
            (
                ('document_id', '__document_id__'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_sequencing_profile(
        self, record: PowerSequencingProfile
    ) -> None:
        self.sequencing_profiles.save(record)

    def get_sequencing_profile(
        self, rid: str
    ) -> PowerSequencingProfile | None:
        return self.sequencing_profiles.get(rid)

    def save_sequence_event(self, record: PowerSequenceEvent) -> None:
        self.sequence_events.save(record)

    def get_sequence_event(
        self, rid: str
    ) -> PowerSequenceEvent | None:
        return self.sequence_events.get(rid)

    def save_ups_transition(self, record: UpsTransitionRecord) -> None:
        self.ups_transitions.save(record)

    def get_ups_transition(
        self, rid: str
    ) -> UpsTransitionRecord | None:
        return self.ups_transitions.get(rid)

    def save_pq_measurement(
        self, record: PowerQualityMeasurement
    ) -> None:
        self.pq_measurements.save(record)

    def get_pq_measurement(
        self, rid: str
    ) -> PowerQualityMeasurement | None:
        return self.pq_measurements.get(rid)

    def save_pq_qualification(
        self, record: PowerQualityQualification
    ) -> None:
        self.pq_qualifications.save(record)

    def get_pq_qualification(
        self, rid: str
    ) -> PowerQualityQualification | None:
        return self.pq_qualifications.get(rid)

    def save_emc_profile(self, record: EmcProductProfile) -> None:
        self.emc_profiles.save(record)

    def get_emc_profile(self, rid: str) -> EmcProductProfile | None:
        return self.emc_profiles.get(rid)

    def save_emc_symptom(self, record: EmcSymptomRecord) -> None:
        self.emc_symptoms.save(record)

    def get_emc_symptom(self, rid: str) -> EmcSymptomRecord | None:
        return self.emc_symptoms.get(rid)
