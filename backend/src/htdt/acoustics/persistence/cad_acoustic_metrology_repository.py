"""Append-only persistence for REV59-AUDIOMET authorities.

Eight tables in one repository — receiver reference point (#774),
fixture scattering (#743), echo diagnostics (#773), DRR (#678).
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from ...cad_repository import SceneRepository
from ...cad_schema import connect_sqlite, require_native_tables
from ...canonical_json import canonical_sha256
from ...cad_receiver_reference import (
    MicrophoneCapsulePose,
    ReceiverReferencePoint,
)
from ..domain.cad_fixture_scattering import (
    FixtureScatteringEvidence,
    MeasurementFixture,
)
from ...cad_echo_diagnostic import (
    DiscreteReflectionEvent,
    EchoDiagnostic,
)
from ...cad_drr_authority import (
    DRRMeasurement,
    DRRMethodProfile,
)


class AcousticMetrologyConflictError(ValueError):
    """An AUDIOMET save violated append-only identity rules."""


class AcousticMetrologyIntegrityError(ValueError):
    """A stored AUDIOMET row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise AcousticMetrologyIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise AcousticMetrologyIntegrityError(
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
            raise AcousticMetrologyConflictError(
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
            raise AcousticMetrologyIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise AcousticMetrologyIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise AcousticMetrologyIntegrityError(
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
                raise AcousticMetrologyIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload'
                )
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadAcousticMetrologyRepository:
    """Native storage for the #774/#743/#773/#678 authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_receiver_reference_points',
                'cad_microphone_capsule_poses',
                'cad_measurement_fixtures',
                'cad_fixture_scattering_evidence',
                'cad_discrete_reflection_events',
                'cad_echo_diagnostics',
                'cad_drr_method_profiles',
                'cad_drr_measurements',
            )

        self.receiver_references = _SealedStore(
            self._connect, 'cad_receiver_reference_points',
            ReceiverReferencePoint, 'reference_id', 'reference_sha256',
            (
                ('document_id', '__document_id__'),
                ('point_kind', 'point_kind'),
            ),
        )

        self.capsule_poses = _SealedStore(
            self._connect, 'cad_microphone_capsule_poses',
            MicrophoneCapsulePose, 'pose_id', 'pose_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('reference_ref_id', 'reference_ref'),
            ),
        )

        self.fixtures = _SealedStore(
            self._connect, 'cad_measurement_fixtures',
            MeasurementFixture, 'fixture_id', 'fixture_sha256',
            (
                ('document_id', '__document_id__'),
                ('fixture_kind', 'fixture_kind'),
            ),
        )

        self.scattering_evidence = _SealedStore(
            self._connect, 'cad_fixture_scattering_evidence',
            FixtureScatteringEvidence, 'evidence_id', 'evidence_sha256',
            (
                ('document_id', '__document_id__'),
                ('bound_kind', 'bound_kind'),
            ),
        )

        self.reflection_events = _SealedStore(
            self._connect, 'cad_discrete_reflection_events',
            DiscreteReflectionEvent, 'event_id', 'event_sha256',
            (
                ('document_id', '__document_id__'),
                ('periodicity', 'periodicity'),
            ),
        )

        self.echo_diagnostics = _SealedStore(
            self._connect, 'cad_echo_diagnostics',
            EchoDiagnostic, 'diagnostic_id', 'diagnostic_sha256',
            (
                ('document_id', '__document_id__'),
                ('signal_class', 'signal_class'),
                ('verdict', 'verdict'),
            ),
        )

        self.drr_methods = _SealedStore(
            self._connect, 'cad_drr_method_profiles',
            DRRMethodProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('receiver_kind', 'receiver_kind'),
            ),
        )

        self.drr_measurements = _SealedStore(
            self._connect, 'cad_drr_measurements',
            DRRMeasurement, 'measurement_id', 'measurement_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('method_ref_id', 'method_ref'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_receiver_reference(self, record: ReceiverReferencePoint) -> None:
        self.receiver_references.save(record)

    def get_receiver_reference(self, rid: str) -> ReceiverReferencePoint | None:
        return self.receiver_references.get(rid)

    def save_capsule_pose(self, record: MicrophoneCapsulePose) -> None:
        self.capsule_poses.save(record)

    def get_capsule_pose(self, rid: str) -> MicrophoneCapsulePose | None:
        return self.capsule_poses.get(rid)

    def save_fixture(self, record: MeasurementFixture) -> None:
        self.fixtures.save(record)

    def get_fixture(self, rid: str) -> MeasurementFixture | None:
        return self.fixtures.get(rid)

    def save_scattering_evidence(self, record: FixtureScatteringEvidence) -> None:
        self.scattering_evidence.save(record)

    def get_scattering_evidence(self, rid: str) -> FixtureScatteringEvidence | None:
        return self.scattering_evidence.get(rid)

    def save_reflection_event(self, record: DiscreteReflectionEvent) -> None:
        self.reflection_events.save(record)

    def get_reflection_event(self, rid: str) -> DiscreteReflectionEvent | None:
        return self.reflection_events.get(rid)

    def save_echo_diagnostic(self, record: EchoDiagnostic) -> None:
        self.echo_diagnostics.save(record)

    def get_echo_diagnostic(self, rid: str) -> EchoDiagnostic | None:
        return self.echo_diagnostics.get(rid)

    def save_drr_method(self, record: DRRMethodProfile) -> None:
        self.drr_methods.save(record)

    def get_drr_method(self, rid: str) -> DRRMethodProfile | None:
        return self.drr_methods.get(rid)

    def save_drr_measurement(self, record: DRRMeasurement) -> None:
        self.drr_measurements.save(record)

    def get_drr_measurement(self, rid: str) -> DRRMeasurement | None:
        return self.drr_measurements.get(rid)
