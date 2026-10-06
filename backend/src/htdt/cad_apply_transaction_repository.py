"""Append-only persistence for the REV59-APPLY authority (#723).

Seven tables in one repository:

* :class:`CadApplyTransactionRepository` — apply capability profiles,
  plans, write records, verifications, rollback plans/executions and
  transaction lifecycle records.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_apply_transaction import (
    ApplyCapabilityProfile,
    ApplyVerificationRecord,
    ApplyWriteRecord,
    DeviceApplyPlan,
    DeviceApplyTransaction,
    RollbackExecutionRecord,
    RollbackPlan,
)


class ApplyTransactionConflictError(ValueError):
    """An apply-transaction save violated append-only identity rules."""


class ApplyTransactionIntegrityError(ValueError):
    """A stored apply-transaction row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise ApplyTransactionIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise ApplyTransactionIntegrityError(
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
        # (column_name, payload path) — path may be dotted for nested
        # refs (``plan_ref.ref_id``); ``__len__`` selects len(record).
        self.columns = columns

    def _column_value(self, record: Any, path: str) -> Any:
        if path == '__document_id__':
            return record.document_id
        value: Any = record
        for part in path.split('.'):
            value = getattr(value, part)
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
            raise ApplyTransactionConflictError(
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
            raise ApplyTransactionIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise ApplyTransactionIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            if record.__dict__.get(path.split('.')[0]) is None:
                continue
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise ApplyTransactionIntegrityError(
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


class CadApplyTransactionRepository:
    """Native storage for the #723 apply-transaction authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_apply_capability_profiles',
                'cad_apply_plans',
                'cad_apply_write_records',
                'cad_apply_verifications',
                'cad_apply_rollback_plans',
                'cad_apply_rollback_executions',
                'cad_apply_transactions',
            )
        self.profiles = _SealedStore(
            self._connect, 'cad_apply_capability_profiles',
            ApplyCapabilityProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('device_ref_id', 'device_ref'),
                ('capability_evidence', 'capability_evidence'),
            ),
        )
        self.plans = _SealedStore(
            self._connect, 'cad_apply_plans',
            DeviceApplyPlan, 'plan_id', 'plan_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('device_ref_id', 'device_ref'),
                _ref('capability_ref_id', 'capability_ref'),
                ('pre_state_evidence', 'pre_state_evidence'),
                ('rollback_strategy', 'rollback_strategy'),
            ),
        )
        self.writes = _SealedStore(
            self._connect, 'cad_apply_write_records',
            ApplyWriteRecord, 'write_id', 'write_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('plan_ref_id', 'plan_ref'),
                ('sequence_index', 'sequence_index'),
                ('write_kind', 'write_kind'),
                ('outcome', 'outcome'),
            ),
        )
        self.verifications = _SealedStore(
            self._connect, 'cad_apply_verifications',
            ApplyVerificationRecord, 'verification_id',
            'verification_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('plan_ref_id', 'plan_ref'),
                ('readback_means', 'readback_means'),
            ),
        )
        self.rollback_plans = _SealedStore(
            self._connect, 'cad_apply_rollback_plans',
            RollbackPlan, 'rollback_plan_id', 'rollback_plan_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('plan_ref_id', 'plan_ref'),
                ('pre_state_evidence', 'pre_state_evidence'),
                ('claim', 'claim'),
            ),
        )
        self.rollback_executions = _SealedStore(
            self._connect, 'cad_apply_rollback_executions',
            RollbackExecutionRecord, 'execution_id', 'execution_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('rollback_plan_ref_id', 'rollback_plan_ref'),
                ('outcome', 'outcome'),
            ),
        )
        self.transactions = _SealedStore(
            self._connect, 'cad_apply_transactions',
            DeviceApplyTransaction, 'transaction_id',
            'transaction_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('plan_ref_id', 'plan_ref'),
                _ref('capability_ref_id', 'capability_ref'),
                ('state_verdict', 'state_verdict'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # Wrappers used by the audit replay chain and callers.
    def save_profile(self, record: ApplyCapabilityProfile) -> None:
        self.profiles.save(record)

    def get_profile(
        self, profile_id: str
    ) -> ApplyCapabilityProfile | None:
        return self.profiles.get(profile_id)

    def save_plan(self, record: DeviceApplyPlan) -> None:
        self.plans.save(record)

    def get_plan(self, plan_id: str) -> DeviceApplyPlan | None:
        return self.plans.get(plan_id)

    def save_write(self, record: ApplyWriteRecord) -> None:
        self.writes.save(record)

    def get_write(self, write_id: str) -> ApplyWriteRecord | None:
        return self.writes.get(write_id)

    def save_verification(self, record: ApplyVerificationRecord) -> None:
        self.verifications.save(record)

    def get_verification(
        self, verification_id: str
    ) -> ApplyVerificationRecord | None:
        return self.verifications.get(verification_id)

    def save_rollback_plan(self, record: RollbackPlan) -> None:
        self.rollback_plans.save(record)

    def get_rollback_plan(
        self, rollback_plan_id: str
    ) -> RollbackPlan | None:
        return self.rollback_plans.get(rollback_plan_id)

    def save_rollback_execution(
        self, record: RollbackExecutionRecord
    ) -> None:
        self.rollback_executions.save(record)

    def get_rollback_execution(
        self, execution_id: str
    ) -> RollbackExecutionRecord | None:
        return self.rollback_executions.get(execution_id)

    def save_transaction(self, record: DeviceApplyTransaction) -> None:
        self.transactions.save(record)

    def get_transaction(
        self, transaction_id: str
    ) -> DeviceApplyTransaction | None:
        return self.transactions.get(transaction_id)
