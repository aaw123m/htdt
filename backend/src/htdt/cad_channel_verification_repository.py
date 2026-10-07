"""Append-only persistence for the #876 channel-verification authority.

Four sealed tables in one repository, mirroring the
``cad_geometry_intake_repository`` pattern:

* ``cad_channel_verification_plans`` — sealed verification plans (cvpl-);
* ``cad_channel_excitation_results`` — per-channel excitation + signature
  analysis outcomes (cvex-);
* ``cad_channel_operator_attestations`` — operator fallback evidence
  (cvoa-); permanently manual, never promoted;
* ``cad_channel_verification_verdicts`` — map-level verdicts (cvvd-).
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_channel_verification import (
    ChannelExcitationResult,
    ChannelVerificationPlan,
    ChannelVerificationVerdict,
    OperatorChannelAttestation,
)


class ChannelVerificationConflictError(ValueError):
    """A channel-verification save violated append-only identity rules."""


class ChannelVerificationIntegrityError(ValueError):
    """A stored channel-verification row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(
        record.identity_payload()  # type: ignore[attr-defined]
    )
    if getattr(record, sha_field) != sha:
        raise ChannelVerificationIntegrityError(
            'record payload does not match its sealed sha256')
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise ChannelVerificationIntegrityError(
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
            raise ChannelVerificationConflictError(
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
            raise ChannelVerificationIntegrityError(
                f'stored {self.table} id disagrees with its payload')
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise ChannelVerificationIntegrityError(
                f'stored {self.table} sha disagrees with its payload')
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise ChannelVerificationIntegrityError(
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
                raise ChannelVerificationIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload')
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


_TABLES = (
    'cad_channel_verification_plans',
    'cad_channel_excitation_results',
    'cad_channel_operator_attestations',
    'cad_channel_verification_verdicts',
)


class CadChannelVerificationRepository:
    """Append-only store for the four channel-verification record kinds."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, *_TABLES)
        self._plans = _SealedStore(
            self._connect,
            'cad_channel_verification_plans',
            ChannelVerificationPlan,
            'plan_id',
            'plan_sha256',
            (
                ('document_id', '__document_id__'),
                ('reference_channel', 'reference_channel'),
                ('target_count', 'target_count'),
                ('method', 'method'),
                ('created_at_utc', 'created_at_utc'),
            ),
        )
        self._results = _SealedStore(
            self._connect,
            'cad_channel_excitation_results',
            ChannelExcitationResult,
            'result_id',
            'result_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('plan_ref_id', 'plan_ref'),
                ('logical_channel', 'logical_channel'),
                _ref('acquisition_run_ref_id', 'acquisition_run_ref'),
                ('capture_quality', 'capture_quality'),
                ('response_detected', 'response_detected'),
                ('measured_at_utc', 'measured_at_utc'),
            ),
        )
        self._attestations = _SealedStore(
            self._connect,
            'cad_channel_operator_attestations',
            OperatorChannelAttestation,
            'attestation_id',
            'attestation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('plan_ref_id', 'plan_ref'),
                ('logical_channel', 'logical_channel'),
                ('attested_by', 'attested_by'),
                ('responded', 'responded'),
                ('attested_at_utc', 'attested_at_utc'),
            ),
        )
        self._verdicts = _SealedStore(
            self._connect,
            'cad_channel_verification_verdicts',
            ChannelVerificationVerdict,
            'verdict_id',
            'verdict_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('plan_ref_id', 'plan_ref'),
                ('map_state', 'map_state'),
                ('evaluated_at_utc', 'evaluated_at_utc'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # -- plans ---------------------------------------------------------

    def save_plan(self, record: ChannelVerificationPlan) -> None:
        self._plans.save(record)

    def get_plan(self, plan_id: str) -> ChannelVerificationPlan | None:
        return self._plans.get(plan_id)

    def list_plans(
        self, document_id: str | None = None,
    ) -> tuple[ChannelVerificationPlan, ...]:
        return self._plans.list(document_id)

    # -- excitation results ---------------------------------------------

    def save_excitation_result(
        self, record: ChannelExcitationResult,
    ) -> None:
        self._results.save(record)

    def get_excitation_result(
        self, result_id: str,
    ) -> ChannelExcitationResult | None:
        return self._results.get(result_id)

    def list_excitation_results(
        self, document_id: str | None = None,
    ) -> tuple[ChannelExcitationResult, ...]:
        return self._results.list(document_id)

    # -- operator attestations ------------------------------------------

    def save_attestation(
        self, record: OperatorChannelAttestation,
    ) -> None:
        self._attestations.save(record)

    def get_attestation(
        self, attestation_id: str,
    ) -> OperatorChannelAttestation | None:
        return self._attestations.get(attestation_id)

    def list_attestations(
        self, document_id: str | None = None,
    ) -> tuple[OperatorChannelAttestation, ...]:
        return self._attestations.list(document_id)

    # -- verdicts --------------------------------------------------------

    def save_verdict(self, record: ChannelVerificationVerdict) -> None:
        self._verdicts.save(record)

    def get_verdict(
        self, verdict_id: str,
    ) -> ChannelVerificationVerdict | None:
        return self._verdicts.get(verdict_id)

    def list_verdicts(
        self, document_id: str | None = None,
    ) -> tuple[ChannelVerificationVerdict, ...]:
        return self._verdicts.list(document_id)
