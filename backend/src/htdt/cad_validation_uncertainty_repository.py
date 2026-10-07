"""Append-only persistence for the #810 validation-uncertainty authority.

Three tables in one repository — preregistered validation protocols,
per-observable uncertainty evaluations, and whole-study validation
verdicts:

* ``cad_validation_uncertainty_protocols``
* ``cad_observable_uncertainty_evaluations``
* ``cad_uncertainty_validation_verdicts``

The stores are append-only and re-verify seal/id on every read. Beyond
the generic integrity checks, this repository enforces *authority
reachability* at write time: an evaluation can only be stored if the
protocol it binds is already stored with the same sha256, and a verdict
can only be stored if every bound evaluation exists and matches its
pinned snapshot — a verdict can never restate evidence it did not see.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_validation_uncertainty import (
    ObservableUncertaintyEvaluation,
    UncertaintyValidationVerdict,
    ValidationUncertaintyProtocol,
)


class ValidationUncertaintyConflictError(ValueError):
    """A validation-uncertainty save violated append-only identity rules."""


class ValidationUncertaintyIntegrityError(ValueError):
    """A stored validation-uncertainty row disagreed with its payload."""


class ValidationUncertaintyAuthorityError(ValueError):
    """A record referenced authority that is not stored or disagrees."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise ValidationUncertaintyIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise ValidationUncertaintyIntegrityError(
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
            raise ValidationUncertaintyConflictError(
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
            raise ValidationUncertaintyIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise ValidationUncertaintyIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise ValidationUncertaintyIntegrityError(
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
                raise ValidationUncertaintyIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload'
                )
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadValidationUncertaintyRepository:
    """Native storage for the #810 uncertainty-aware validation authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_validation_uncertainty_protocols',
                'cad_observable_uncertainty_evaluations',
                'cad_uncertainty_validation_verdicts',
            )
        self.protocols = _SealedStore(
            self._connect, 'cad_validation_uncertainty_protocols',
            ValidationUncertaintyProtocol, 'protocol_id', 'protocol_sha256',
            (
                ('document_id', '__document_id__'),
                ('model_id', 'model_id'),
                ('protocol_version', 'protocol_version'),
                ('calibration_uncertainty_tuned', 'calibration_uncertainty_tuned'),
            ),
        )
        self.evaluations = _SealedStore(
            self._connect, 'cad_observable_uncertainty_evaluations',
            ObservableUncertaintyEvaluation, 'evaluation_id',
            'evaluation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('protocol_ref_id', 'protocol_ref'),
                ('candidate_id', 'candidate_id'),
                ('split', 'split'),
                ('observable_id', 'observable.observable_id'),
                ('summary_verdict', 'summary_verdict'),
                ('dominant_uncertainty_category', 'dominant_uncertainty_category'),
            ),
        )
        self.verdicts = _SealedStore(
            self._connect, 'cad_uncertainty_validation_verdicts',
            UncertaintyValidationVerdict, 'verdict_id', 'verdict_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('protocol_ref_id', 'protocol_ref'),
                _ref('validation_ref_id', 'validation_ref'),
                ('evidence_scope', 'evidence_scope'),
                ('absolute_verdict', 'absolute_verdict'),
                ('ranking_verdict', 'ranking_verdict'),
                ('calibration_state', 'calibration_state'),
                ('model_form_discrepancy_suspected',
                 'model_form_discrepancy_suspected'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # protocols
    def save_protocol(self, record: ValidationUncertaintyProtocol) -> None:
        self.protocols.save(record)

    def get_protocol(self, rid: str) -> ValidationUncertaintyProtocol | None:
        return self.protocols.get(rid)

    def list_protocols(
        self, document_id: str | None = None
    ) -> tuple[ValidationUncertaintyProtocol, ...]:
        return self.protocols.list(document_id)

    # evaluations
    def save_evaluation(self, record: ObservableUncertaintyEvaluation) -> None:
        """Store an evaluation only if its bound protocol is stored and
        agrees on identity — authority must be reachable, not just named."""
        protocol = self.get_protocol(record.protocol_ref.ref_id)
        if protocol is None:
            raise ValidationUncertaintyAuthorityError(
                'evaluation binds a protocol that is not stored'
            )
        if protocol.protocol_sha256 != record.protocol_ref.ref_sha256:
            raise ValidationUncertaintyAuthorityError(
                'evaluation binds a different protocol sha256 than stored'
            )
        if record.protocol_version != protocol.protocol_version:
            raise ValidationUncertaintyAuthorityError(
                'evaluation restates the protocol version it binds'
            )
        declared = protocol.observable(record.observable.observable_id)
        if declared != record.observable:
            raise ValidationUncertaintyAuthorityError(
                'evaluation restates the observable metric the protocol '
                'sealed — it cannot weaken the declared contract'
            )
        self.evaluations.save(record)

    def get_evaluation(
        self, rid: str
    ) -> ObservableUncertaintyEvaluation | None:
        return self.evaluations.get(rid)

    def list_evaluations(
        self, document_id: str | None = None
    ) -> tuple[ObservableUncertaintyEvaluation, ...]:
        return self.evaluations.list(document_id)

    # verdicts
    def save_verdict(self, record: UncertaintyValidationVerdict) -> None:
        """Store a verdict only if every bound evaluation exists and
        matches its pinned snapshot field-for-field."""
        protocol = self.get_protocol(record.protocol_ref.ref_id)
        if protocol is None:
            raise ValidationUncertaintyAuthorityError(
                'verdict binds a protocol that is not stored'
            )
        if protocol.protocol_sha256 != record.protocol_ref.ref_sha256:
            raise ValidationUncertaintyAuthorityError(
                'verdict binds a different protocol sha256 than stored'
            )
        if record.protocol_version != protocol.protocol_version:
            raise ValidationUncertaintyAuthorityError(
                'verdict restates the protocol version it binds'
            )
        if record.protocol_observable_ids != tuple(
            metric.observable_id for metric in protocol.observable_metrics
        ):
            raise ValidationUncertaintyAuthorityError(
                'verdict restates the protocol observable set it binds — '
                'coverage is judged against the sealed declaration'
            )
        for binding in record.evaluation_bindings:
            evaluation = self.get_evaluation(binding.evaluation_ref.ref_id)
            if evaluation is None:
                raise ValidationUncertaintyAuthorityError(
                    'verdict binds an evaluation that is not stored'
                )
            if (
                evaluation.evaluation_sha256
                != binding.evaluation_ref.ref_sha256
            ):
                raise ValidationUncertaintyAuthorityError(
                    'verdict binds a different evaluation sha256 than stored'
                )
            mismatched = (
                evaluation.observable.observable_id != binding.observable_id
                or evaluation.candidate_id != binding.candidate_id
                or evaluation.split != binding.split
                or evaluation.summary_verdict != binding.summary_verdict
                or evaluation.dominant_uncertainty_category
                != binding.dominant_uncertainty_category
            )
            if mismatched:
                raise ValidationUncertaintyAuthorityError(
                    'verdict binding restates stored evaluation fields — '
                    'a verdict cannot misreport the evidence it binds'
                )
        self.verdicts.save(record)

    def get_verdict(self, rid: str) -> UncertaintyValidationVerdict | None:
        return self.verdicts.get(rid)

    def list_verdicts(
        self, document_id: str | None = None
    ) -> tuple[UncertaintyValidationVerdict, ...]:
        return self.verdicts.list(document_id)
