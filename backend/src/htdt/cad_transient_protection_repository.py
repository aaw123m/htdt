"""Append-only persistence for the #789 transient-protection authority.

Six tables in one repository — protected paths
(``cad_protected_paths``), declared plans
(``cad_transient_protection_plans``), SPD evidence
(``cad_spd_evidence``), health observations
(``cad_transient_protection_observations``), staling events
(``cad_transient_protection_events``) and sealed per-path assessments
(``cad_transient_protection_assessments``).
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .cad_transient_protection import (
    ProtectedPath,
    SPDEvidence,
    TransientProtectionAssessment,
    TransientProtectionEvent,
    TransientProtectionIntegrityError,
    TransientProtectionObservation,
    TransientProtectionPlan,
)
from .canonical_json import canonical_sha256


class TransientProtectionConflictError(ValueError):
    """A transient-protection save violated append-only identity rules."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise TransientProtectionIntegrityError(
            'record payload does not match its sealed sha256')
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise TransientProtectionIntegrityError(
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

    def save(self, record: Any) -> None:
        _assert_sealed(record, self.sha_field, self.id_field)
        rid = getattr(record, self.id_field)
        existing = self.get(rid)
        if existing is not None:
            if getattr(existing, self.sha_field) == getattr(
                    record, self.sha_field):
                return
            raise TransientProtectionConflictError(
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
            raise TransientProtectionIntegrityError(
                f'stored {self.table} id disagrees with its payload')
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise TransientProtectionIntegrityError(
                f'stored {self.table} sha disagrees with its payload')
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise TransientProtectionIntegrityError(
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
                raise TransientProtectionIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload')
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadTransientProtectionRepository:
    """Native storage for the #789 transient-protection authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_protected_paths',
                'cad_transient_protection_plans',
                'cad_spd_evidence',
                'cad_transient_protection_observations',
                'cad_transient_protection_events',
                'cad_transient_protection_assessments',
            )
        self.protected_paths = _SealedStore(
            self._connect,
            'cad_protected_paths',
            ProtectedPath, 'path_id', 'path_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('load_ref_id', 'load_ref'),
                ('path_kind', 'path_kind'),
                ('requires_protection', 'requires_protection'),
            ),
        )
        self.plans = _SealedStore(
            self._connect,
            'cad_transient_protection_plans',
            TransientProtectionPlan, 'plan_id', 'plan_sha256',
            (
                ('document_id', '__document_id__'),
                ('jurisdiction_country', 'jurisdiction_country'),
                ('code_family', 'code_family'),
                ('code_edition', 'code_edition'),
                ('requires_qualified_review', 'requires_qualified_review'),
            ),
        )
        self.spd_evidence = _SealedStore(
            self._connect,
            'cad_spd_evidence',
            SPDEvidence, 'spd_id', 'spd_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('path_ref_id', 'path_ref'),
                ('domain', 'domain'),
                ('spd_type_class', 'spd_type_class'),
                ('standard_profile', 'standard_profile'),
                ('install_state', 'install_state'),
                ('evidence_basis', 'evidence_basis'),
            ),
        )
        self.observations = _SealedStore(
            self._connect,
            'cad_transient_protection_observations',
            TransientProtectionObservation,
            'observation_id', 'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('spd_ref_id', 'spd_ref'),
                ('status', 'status'),
                ('status_source', 'status_source'),
                ('observed_at_utc', 'observed_at_utc'),
            ),
        )
        self.events = _SealedStore(
            self._connect,
            'cad_transient_protection_events',
            TransientProtectionEvent, 'event_id', 'event_sha256',
            (
                ('document_id', '__document_id__'),
                ('event_kind', 'event_kind'),
                ('observed_at_utc', 'observed_at_utc'),
            ),
        )
        self.assessments = _SealedStore(
            self._connect,
            'cad_transient_protection_assessments',
            TransientProtectionAssessment,
            'assessment_id', 'assessment_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('path_ref_id', 'path_ref'),
                _ref('plan_ref_id', 'plan_ref'),
                ('verdict', 'verdict'),
                ('coordination_state', 'coordination_state'),
                ('professional_review_required',
                 'professional_review_required'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def get_path(self, path_id: str) -> ProtectedPath | None:
        return self.protected_paths.get(path_id)

    def get_plan(
            self, plan_id: str) -> TransientProtectionPlan | None:
        return self.plans.get(plan_id)

    def get_spd(self, spd_id: str) -> SPDEvidence | None:
        return self.spd_evidence.get(spd_id)

    def get_observation(
            self, observation_id: str,
    ) -> TransientProtectionObservation | None:
        return self.observations.get(observation_id)

    def get_event(
            self, event_id: str) -> TransientProtectionEvent | None:
        return self.events.get(event_id)

    def get_assessment(
            self, assessment_id: str,
    ) -> TransientProtectionAssessment | None:
        return self.assessments.get(assessment_id)
