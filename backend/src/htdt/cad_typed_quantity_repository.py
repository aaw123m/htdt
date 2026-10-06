"""Append-only persistence for the typed physical-quantity authority
(#728, REV59-UNITS).

Two tables:

* ``cad_typed_quantities`` — sealed typed physical quantities
  (SI-canonical storage + preserved source observation).
* ``cad_quantity_operations`` — sealed typed-operation verdicts
  (convert/compare/combine/ratio etc.).
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_typed_quantity import (
    CadQuantityOperation,
    CadTypedQuantity,
)


class TypedQuantityConflictError(ValueError):
    """A typed-quantity save violated append-only identity rules."""


class TypedQuantityIntegrityError(ValueError):
    """A stored typed-quantity row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise TypedQuantityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise TypedQuantityIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadTypedQuantityRepository:
    """Native storage for the #728 typed-quantity authority records."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_typed_quantities',
                'cad_quantity_operations',
            )

    # ------------------------------------------------------------------
    # Quantities

    def save_quantity(self, quantity: CadTypedQuantity) -> None:
        _assert_sealed(quantity, 'quantity_sha256', 'quantity_id')
        existing = self.get_quantity(quantity.quantity_id)
        if existing is not None:
            if existing.quantity_sha256 == quantity.quantity_sha256:
                return
            raise TypedQuantityConflictError(
                'typed quantities are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_typed_quantities (
                    quantity_id, quantity_sha256, document_id,
                    quantity_kind, value_kind, canonical_value,
                    canonical_unit, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    quantity.quantity_id,
                    quantity.quantity_sha256,
                    quantity.document_id,
                    quantity.quantity_kind,
                    quantity.value_kind,
                    quantity.canonical_value,
                    quantity.canonical_unit,
                    quantity.declared_at_utc,
                    quantity.model_dump_json(),
                ),
            )

    def get_quantity(
        self, quantity_id: str
    ) -> CadTypedQuantity | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_typed_quantities WHERE quantity_id=?',
                (quantity_id,),
            ).fetchone()
        if row is None:
            return None
        quantity = CadTypedQuantity.model_validate_json(
            row['payload_json']
        )
        if (
            quantity.quantity_id != row['quantity_id']
            or quantity.quantity_sha256 != row['quantity_sha256']
            or quantity.document_id != row['document_id']
            or quantity.quantity_kind != row['quantity_kind']
            or quantity.value_kind != row['value_kind']
            or quantity.canonical_value != row['canonical_value']
            or quantity.canonical_unit != row['canonical_unit']
            or quantity.declared_at_utc != row['declared_at_utc']
        ):
            raise TypedQuantityIntegrityError(
                'typed quantity row disagrees with payload'
            )
        return quantity

    def list_quantities(
        self, document_id: str
    ) -> tuple[CadTypedQuantity, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_typed_quantities '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadTypedQuantity.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Operations

    def save_operation(self, operation: CadQuantityOperation) -> None:
        _assert_sealed(operation, 'operation_sha256', 'operation_id')
        existing = self.get_operation(operation.operation_id)
        if existing is not None:
            if existing.operation_sha256 == operation.operation_sha256:
                return
            raise TypedQuantityConflictError(
                'quantity operation verdicts are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_quantity_operations (
                    operation_id, operation_sha256, document_id,
                    operation, state, evaluation_version,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation.operation_id,
                    operation.operation_sha256,
                    operation.document_id,
                    operation.operation,
                    operation.state,
                    operation.evaluation_version,
                    operation.evaluated_at_utc,
                    operation.model_dump_json(),
                ),
            )

    def get_operation(
        self, operation_id: str
    ) -> CadQuantityOperation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_quantity_operations '
                'WHERE operation_id=?',
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        operation = CadQuantityOperation.model_validate_json(
            row['payload_json']
        )
        if (
            operation.operation_id != row['operation_id']
            or operation.operation_sha256 != row['operation_sha256']
            or operation.document_id != row['document_id']
            or operation.operation != row['operation']
            or operation.state != row['state']
            or operation.evaluation_version != row['evaluation_version']
            or operation.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise TypedQuantityIntegrityError(
                'quantity operation row disagrees with payload'
            )
        return operation

    def list_operations(
        self, document_id: str
    ) -> tuple[CadQuantityOperation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_quantity_operations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadQuantityOperation.model_validate_json(r['payload_json'])
            for r in rows
        )


__all__ = [
    'CadTypedQuantityRepository',
    'TypedQuantityConflictError',
    'TypedQuantityIntegrityError',
]
