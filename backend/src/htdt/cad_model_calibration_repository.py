"""Append-only R180 calibration persistence (#948 / #522).

Model-calibration authorities are durable project records, not in-memory
byproducts: specs, results, freezes and materialized calibrated models are
stored by exact semantic identity, and every evidence-consumption event
(training/development/holdout) is recorded so holdout-independence verdicts
derive from persisted history instead of a caller-supplied list.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_model_calibration import (
    AcousticModelCalibrationResult,
    AcousticModelCalibrationSpec,
    CalibratedAcousticModel,
    CalibratedModelFreeze,
    HoldoutDisciplineRecord,
    evaluate_holdout_discipline,
)
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite
from .r120_geometry_compiler import ExactExternalAuthorityRef
from .clock import utc_now_iso as _utc_now


class CalibrationConflictError(ValueError):
    """A calibration authority was saved twice with different content."""


class CadModelCalibrationRepository:
    """Durable authority store for the R180 calibration lifecycle."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_calibration_specs',
                'cad_calibration_results',
                'cad_calibration_models',
                'cad_calibration_freezes',
                'cad_calibration_holdout_records',
                'cad_calibration_evidence_events',
            )

    def save_spec(self, spec: AcousticModelCalibrationSpec) -> None:
        self._insert_once(
            table='cad_calibration_specs',
            key_column='spec_id',
            key=spec.spec_id,
            columns=(
                'spec_id',
                'semantic_sha256',
                'baseline_snapshot_sha256',
                'solver_id',
                'payload_json',
                'recorded_at_utc',
            ),
            values=(
                spec.spec_id,
                spec.semantic_sha256,
                spec.baseline_snapshot_sha256,
                spec.solver_id,
                spec.model_dump_json(),
                _utc_now(),
            ),
            payload=spec.model_dump_json(),
        )

    def get_spec(
        self, spec_id: str
    ) -> AcousticModelCalibrationSpec | None:
        row = self._select_row('cad_calibration_specs', 'spec_id', spec_id)
        if row is None:
            return None
        spec = AcousticModelCalibrationSpec.model_validate_json(
            row['payload_json']
        )
        if (
            row['spec_id'] != spec.spec_id
            or row['semantic_sha256'] != spec.semantic_sha256
            or row['baseline_snapshot_sha256'] != spec.baseline_snapshot_sha256
            or row['solver_id'] != spec.solver_id
        ):
            raise ValueError(
                'persisted calibration spec row disagrees with its payload'
            )
        return spec

    # -- results -------------------------------------------------------

    def save_result(self, result: AcousticModelCalibrationResult) -> None:
        if self.get_spec(result.spec_id) is None:
            raise ValueError('calibration result pins a spec that is not persisted')
        self._insert_once(
            table='cad_calibration_results',
            key_column='result_id',
            key=result.result_id,
            columns=(
                'result_id',
                'semantic_sha256',
                'spec_id',
                'calibrated_model_sha256',
                'payload_json',
                'recorded_at_utc',
            ),
            values=(
                result.result_id,
                result.semantic_sha256,
                result.spec_id,
                result.calibrated_model_sha256,
                result.model_dump_json(),
                _utc_now(),
            ),
            payload=result.model_dump_json(),
        )

    def get_result(
        self, result_id: str
    ) -> AcousticModelCalibrationResult | None:
        row = self._select_row(
            'cad_calibration_results', 'result_id', result_id
        )
        if row is None:
            return None
        result = AcousticModelCalibrationResult.model_validate_json(
            row['payload_json']
        )
        if (
            row['result_id'] != result.result_id
            or row['semantic_sha256'] != result.semantic_sha256
            or row['spec_id'] != result.spec_id
            or row['calibrated_model_sha256'] != result.calibrated_model_sha256
        ):
            raise ValueError(
                'persisted calibration result row disagrees with its payload'
            )
        return result

    # -- materialized calibrated models --------------------------------

    def save_model(self, model: CalibratedAcousticModel) -> None:
        if self.get_result(model.calibration_result_id) is None:
            raise ValueError(
                'materialized model pins a result that is not persisted'
            )
        self._insert_once(
            table='cad_calibration_models',
            key_column='materialized_model_id',
            key=model.materialized_model_id,
            columns=(
                'materialized_model_id',
                'semantic_sha256',
                'calibration_result_id',
                'baseline_snapshot_sha256',
                'payload_json',
                'recorded_at_utc',
            ),
            values=(
                model.materialized_model_id,
                model.semantic_sha256,
                model.calibration_result_id,
                model.baseline_snapshot_sha256,
                model.model_dump_json(),
                _utc_now(),
            ),
            payload=model.model_dump_json(),
        )

    def get_model(
        self, materialized_model_id: str
    ) -> CalibratedAcousticModel | None:
        row = self._select_row(
            'cad_calibration_models',
            'materialized_model_id',
            materialized_model_id,
        )
        if row is None:
            return None
        model = CalibratedAcousticModel.model_validate_json(
            row['payload_json']
        )
        if (
            row['materialized_model_id'] != model.materialized_model_id
            or row['semantic_sha256'] != model.semantic_sha256
            or row['calibration_result_id'] != model.calibration_result_id
            or row['baseline_snapshot_sha256'] != model.baseline_snapshot_sha256
        ):
            raise ValueError(
                'persisted calibrated model row disagrees with its payload'
            )
        return model

    # -- freezes --------------------------------------------------------

    def save_freeze(self, freeze: CalibratedModelFreeze) -> None:
        if self.get_result(freeze.calibration_result_id) is None:
            raise ValueError('freeze pins a result that is not persisted')
        self._insert_once(
            table='cad_calibration_freezes',
            key_column='freeze_id',
            key=freeze.freeze_id,
            columns=(
                'freeze_id',
                'semantic_sha256',
                'calibration_result_id',
                'calibrated_model_sha256',
                'payload_json',
                'recorded_at_utc',
            ),
            values=(
                freeze.freeze_id,
                freeze.semantic_sha256,
                freeze.calibration_result_id,
                freeze.calibrated_model_sha256,
                freeze.model_dump_json(),
                _utc_now(),
            ),
            payload=freeze.model_dump_json(),
        )

    def get_freeze(
        self, freeze_id: str
    ) -> CalibratedModelFreeze | None:
        row = self._select_row(
            'cad_calibration_freezes', 'freeze_id', freeze_id
        )
        if row is None:
            return None
        freeze = CalibratedModelFreeze.model_validate_json(
            row['payload_json']
        )
        if (
            row['freeze_id'] != freeze.freeze_id
            or row['semantic_sha256'] != freeze.semantic_sha256
            or row['calibration_result_id'] != freeze.calibration_result_id
            or row['calibrated_model_sha256'] != freeze.calibrated_model_sha256
        ):
            raise ValueError(
                'persisted calibration freeze row disagrees with its payload'
            )
        return freeze

    def get_holdout_record(
        self, record_id: str
    ) -> HoldoutDisciplineRecord | None:
        row = self._select_row(
            'cad_calibration_holdout_records', 'record_id', record_id
        )
        if row is None:
            return None
        record = HoldoutDisciplineRecord.model_validate_json(
            row['payload_json']
        )
        if (
            row['record_id'] != record.record_id
            or row['semantic_sha256'] != record.semantic_sha256
            or row['freeze_id'] != record.freeze_id
        ):
            raise ValueError(
                'persisted holdout record row disagrees with its payload'
            )
        return record

    # -- holdout records ---------------------------------------------------

    def get_holdout_record(
        self, record_id: str
    ) -> HoldoutDisciplineRecord | None:
        payload = self._select(
            'cad_calibration_holdout_records', 'record_id', record_id
        )
        if payload is None:
            return None
        return HoldoutDisciplineRecord.model_validate_json(payload)

    # -- evidence-consumption history ------------------------------------

    def record_evidence_consumption(
        self,
        *,
        campaign_ref: ExactExternalAuthorityRef,
        consumption_kind: str,
        freeze_id: str | None = None,
        record_id: str | None = None,
    ) -> None:
        """Persist one training/development/holdout consumption event."""
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_calibration_evidence_events (
                    campaign_id, campaign_sha256, consumption_kind,
                    freeze_id, record_id, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign_ref.authority_id,
                    campaign_ref.semantic_hash_sha256,
                    consumption_kind,
                    freeze_id,
                    record_id,
                    _utc_now(),
                ),
            )

    def consumed_campaign_ids(self) -> tuple[str, ...]:
        """All campaigns already consumed for calibration/development."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT campaign_id
                FROM cad_calibration_evidence_events
                ORDER BY campaign_id
                """
            ).fetchall()
        return tuple(row['campaign_id'] for row in rows)

    def evaluate_persisted_holdout(
        self,
        freeze: CalibratedModelFreeze,
        spec: AcousticModelCalibrationSpec,
        *,
        consumed_campaign_ref: ExactExternalAuthorityRef,
        consumption_kind: str = 'holdout',
    ) -> HoldoutDisciplineRecord:
        """Holdout verdict derived from persisted history, not caller claims.

        The exact freeze must already be persisted — a holdout cannot be
        claimed against a model that was never frozen — and the recorded
        consumption event must postdate the freeze row.
        """
        if self.get_freeze(freeze.freeze_id) is None:
            raise ValueError('holdout evaluated against an unfrozen model')
        previously_consumed = set(self.consumed_campaign_ids())
        record = evaluate_holdout_discipline(
            freeze,
            spec,
            consumed_campaign_ref=consumed_campaign_ref,
            previously_consumed_campaign_ids=previously_consumed,
        )
        self._insert_once(
            table='cad_calibration_holdout_records',
            key_column='record_id',
            key=record.record_id,
            columns=(
                'record_id',
                'semantic_sha256',
                'freeze_id',
                'payload_json',
                'recorded_at_utc',
            ),
            values=(
                record.record_id,
                record.semantic_sha256,
                record.freeze_id,
                record.model_dump_json(),
                _utc_now(),
            ),
            payload=record.model_dump_json(),
        )
        self.record_evidence_consumption(
            campaign_ref=consumed_campaign_ref,
            consumption_kind=consumption_kind,
            freeze_id=freeze.freeze_id,
            record_id=record.record_id,
        )
        return record

    # -- internals ------------------------------------------------------

    def _select(self, table: str, column: str, key: str) -> str | None:
        row = self._select_row(table, column, key)
        return None if row is None else row['payload_json']

    def _select_row(
        self, table: str, column: str, key: str
    ) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            return connection.execute(
                f'SELECT * FROM {table} WHERE {column} = ?',
                (key,),
            ).fetchone()

    def _insert_once(
        self,
        *,
        table: str,
        key_column: str,
        key: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        payload: str,
    ) -> None:
        existing = self._select(table, key_column, key)
        if existing is not None:
            if existing != payload:
                raise CalibrationConflictError(
                    f'{key_column} {key} is persisted with different content'
                )
            return
        placeholders = ', '.join('?' for _ in values)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f'INSERT INTO {table} ({", ".join(columns)}) '
                f'VALUES ({placeholders})',
                values,
            )
