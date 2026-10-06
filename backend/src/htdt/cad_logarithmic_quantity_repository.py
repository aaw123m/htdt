"""Append-only persistence for the typed logarithmic quantity authority
(#691, REV58-IDENT).

Three tables:

* ``cad_log_quantities`` — sealed typed dB/linear quantity values.
* ``cad_log_calibration_bridges`` — sealed cross-domain calibration
  relations.
* ``cad_log_operations`` — sealed typed-operation verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_logarithmic_quantity import (
    CadCalibrationBridge,
    CadLogOperation,
    CadLogQuantity,
)


class LogQuantityConflictError(ValueError):
    """A log-quantity save violated append-only identity rules."""


class LogQuantityIntegrityError(ValueError):
    """A stored log-quantity row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise LogQuantityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise LogQuantityIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadLogQuantityRepository:
    """Native storage for the #691 typed-quantity authority records."""

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
                'cad_log_quantities',
                'cad_log_calibration_bridges',
                'cad_log_operations',
            )

    # ------------------------------------------------------------------
    # Quantities

    def save_quantity(self, quantity: CadLogQuantity) -> None:
        _assert_sealed(quantity, 'quantity_sha256', 'quantity_id')
        existing = self.get_quantity(quantity.quantity_id)
        if existing is not None:
            if existing.quantity_sha256 == quantity.quantity_sha256:
                return
            raise LogQuantityConflictError(
                'log quantities are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_log_quantities (
                    quantity_id, quantity_sha256, document_id,
                    quantity_class, domain, quantity,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    quantity.quantity_id,
                    quantity.quantity_sha256,
                    quantity.document_id,
                    quantity.quantity_class,
                    quantity.domain,
                    quantity.quantity,
                    quantity.declared_at_utc,
                    quantity.model_dump_json(),
                ),
            )

    def get_quantity(
        self, quantity_id: str
    ) -> CadLogQuantity | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_log_quantities WHERE quantity_id=?',
                (quantity_id,),
            ).fetchone()
        if row is None:
            return None
        quantity = CadLogQuantity.model_validate_json(
            row['payload_json']
        )
        if (
            quantity.quantity_id != row['quantity_id']
            or quantity.quantity_sha256 != row['quantity_sha256']
            or quantity.document_id != row['document_id']
            or quantity.quantity_class != row['quantity_class']
            or quantity.domain != row['domain']
            or quantity.quantity != row['quantity']
            or quantity.declared_at_utc != row['declared_at_utc']
        ):
            raise LogQuantityIntegrityError(
                'log quantity row disagrees with payload'
            )
        return quantity

    def list_quantities(
        self, document_id: str
    ) -> tuple[CadLogQuantity, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_log_quantities '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadLogQuantity.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Calibration bridges

    def save_bridge(self, bridge: CadCalibrationBridge) -> None:
        _assert_sealed(bridge, 'bridge_sha256', 'bridge_id')
        existing = self.get_bridge(bridge.bridge_id)
        if existing is not None:
            if existing.bridge_sha256 == bridge.bridge_sha256:
                return
            raise LogQuantityConflictError(
                'calibration bridges are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_log_calibration_bridges (
                    bridge_id, bridge_sha256, document_id,
                    bridge_label, from_domain, to_domain, status,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bridge.bridge_id,
                    bridge.bridge_sha256,
                    bridge.document_id,
                    bridge.bridge_label,
                    bridge.from_domain,
                    bridge.to_domain,
                    bridge.status,
                    bridge.declared_at_utc,
                    bridge.model_dump_json(),
                ),
            )

    def get_bridge(
        self, bridge_id: str
    ) -> CadCalibrationBridge | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_log_calibration_bridges '
                'WHERE bridge_id=?',
                (bridge_id,),
            ).fetchone()
        if row is None:
            return None
        bridge = CadCalibrationBridge.model_validate_json(
            row['payload_json']
        )
        if (
            bridge.bridge_id != row['bridge_id']
            or bridge.bridge_sha256 != row['bridge_sha256']
            or bridge.document_id != row['document_id']
            or bridge.bridge_label != row['bridge_label']
            or bridge.from_domain != row['from_domain']
            or bridge.to_domain != row['to_domain']
            or bridge.status != row['status']
            or bridge.declared_at_utc != row['declared_at_utc']
        ):
            raise LogQuantityIntegrityError(
                'calibration bridge row disagrees with payload'
            )
        return bridge

    def list_bridges(
        self, document_id: str
    ) -> tuple[CadCalibrationBridge, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_log_calibration_bridges '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadCalibrationBridge.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Operations

    def save_operation(self, operation: CadLogOperation) -> None:
        _assert_sealed(operation, 'operation_sha256', 'operation_id')
        existing = self.get_operation(operation.operation_id)
        if existing is not None:
            if existing.operation_sha256 == operation.operation_sha256:
                return
            raise LogQuantityConflictError(
                'log operation verdicts are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_log_operations (
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
    ) -> CadLogOperation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_log_operations WHERE operation_id=?',
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        operation = CadLogOperation.model_validate_json(
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
            raise LogQuantityIntegrityError(
                'log operation row disagrees with payload'
            )
        return operation

    def list_operations(
        self, document_id: str
    ) -> tuple[CadLogOperation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_log_operations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadLogOperation.model_validate_json(r['payload_json'])
            for r in rows
        )


__all__ = [
    'CadLogQuantityRepository',
    'LogQuantityConflictError',
    'LogQuantityIntegrityError',
]
