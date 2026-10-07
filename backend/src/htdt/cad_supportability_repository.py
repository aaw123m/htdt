"""Append-only persistence for the REV62 lifecycle/supportability
authority (#792).

Five tables in one repository:

* ``cad_supportability_profiles``
* ``cad_supportability_dependencies``
* ``cad_lifecycle_risk_observations``
* ``cad_offline_continuity_evidence``
* ``cad_replacement_readiness``
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_supportability import (
    ExternalDependency,
    LifecycleRiskObservation,
    OfflineContinuityEvidence,
    ReplacementReadiness,
    SupportabilityProfile,
)


class SupportabilityConflictError(ValueError):
    """A supportability save violated append-only identity rules."""


class SupportabilityIntegrityError(ValueError):
    """A stored supportability row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise SupportabilityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise SupportabilityIntegrityError(
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
            raise SupportabilityConflictError(
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
            raise SupportabilityIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise SupportabilityIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise SupportabilityIntegrityError(
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
                raise SupportabilityIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload'
                )
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadSupportabilityRepository:
    """Native storage for the #792 lifecycle/supportability authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_supportability_profiles',
                'cad_supportability_dependencies',
                'cad_lifecycle_risk_observations',
                'cad_offline_continuity_evidence',
                'cad_replacement_readiness',
            )

        self.supportability_profiles = _SealedStore(
            self._connect, 'cad_supportability_profiles',
            SupportabilityProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('profile_label', 'profile_label'),
            ),
        )

        self.external_dependencies = _SealedStore(
            self._connect, 'cad_supportability_dependencies',
            ExternalDependency, 'dependency_id', 'dependency_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
                ('function_label', 'function_label'),
                ('dependency_kind', 'dependency_kind'),
            ),
        )

        self.lifecycle_observations = _SealedStore(
            self._connect, 'cad_lifecycle_risk_observations',
            LifecycleRiskObservation, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('subject_ref_id', 'subject_ref'),
                ('kind', 'kind'),
                ('support_state', 'support_state'),
                ('observed_at_utc', 'observed_at_utc'),
            ),
        )

        self.offline_continuity = _SealedStore(
            self._connect, 'cad_offline_continuity_evidence',
            OfflineContinuityEvidence, 'evidence_id', 'evidence_sha256',
            (
                ('document_id', '__document_id__'),
                ('function_label', 'function_label'),
                ('condition', 'condition'),
                ('outcome', 'outcome'),
            ),
        )

        self.replacement_readiness = _SealedStore(
            self._connect, 'cad_replacement_readiness',
            ReplacementReadiness, 'readiness_id', 'readiness_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('subject_ref_id', 'subject_ref'),
                _ref('requalification_ref_id', 'requalification_scope_ref'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_supportability_profile(
        self, record: SupportabilityProfile
    ) -> None:
        self.supportability_profiles.save(record)

    def get_supportability_profile(
        self, rid: str
    ) -> SupportabilityProfile | None:
        return self.supportability_profiles.get(rid)

    def list_supportability_profiles(
        self, document_id: str | None = None
    ) -> tuple[SupportabilityProfile, ...]:
        return self.supportability_profiles.list(document_id)

    def save_external_dependency(self, record: ExternalDependency) -> None:
        self.external_dependencies.save(record)

    def get_external_dependency(
        self, rid: str
    ) -> ExternalDependency | None:
        return self.external_dependencies.get(rid)

    def list_external_dependencies(
        self, document_id: str | None = None
    ) -> tuple[ExternalDependency, ...]:
        return self.external_dependencies.list(document_id)

    def save_lifecycle_observation(
        self, record: LifecycleRiskObservation
    ) -> None:
        self.lifecycle_observations.save(record)

    def get_lifecycle_observation(
        self, rid: str
    ) -> LifecycleRiskObservation | None:
        return self.lifecycle_observations.get(rid)

    def list_lifecycle_observations(
        self, document_id: str | None = None
    ) -> tuple[LifecycleRiskObservation, ...]:
        return self.lifecycle_observations.list(document_id)

    def save_offline_continuity(
        self, record: OfflineContinuityEvidence
    ) -> None:
        self.offline_continuity.save(record)

    def get_offline_continuity(
        self, rid: str
    ) -> OfflineContinuityEvidence | None:
        return self.offline_continuity.get(rid)

    def list_offline_continuity(
        self, document_id: str | None = None
    ) -> tuple[OfflineContinuityEvidence, ...]:
        return self.offline_continuity.list(document_id)

    def save_replacement_readiness(
        self, record: ReplacementReadiness
    ) -> None:
        self.replacement_readiness.save(record)

    def get_replacement_readiness(
        self, rid: str
    ) -> ReplacementReadiness | None:
        return self.replacement_readiness.get(rid)

    def list_replacement_readiness(
        self, document_id: str | None = None
    ) -> tuple[ReplacementReadiness, ...]:
        return self.replacement_readiness.list(document_id)
