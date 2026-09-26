"""Append-only R180 calibration persistence (#948 / #522).

Model-calibration authorities are durable project records, not in-memory
byproducts: specs, results, freezes and materialized calibrated models are
stored by exact semantic identity, and every evidence-consumption event
(training/development/holdout) is recorded so holdout-independence verdicts
derive from persisted history instead of a caller-supplied list.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
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
from .r120_geometry_compiler import ExactExternalAuthorityRef


class CalibrationConflictError(ValueError):
    """A calibration authority was saved twice with different content."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CadModelCalibrationRepository:
    """Durable authority store for the R180 calibration lifecycle."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_calibration_specs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    spec_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    baseline_snapshot_sha256 TEXT NOT NULL,
                    solver_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_calibration_results (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    result_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    spec_id TEXT NOT NULL,
                    calibrated_model_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_calibration_models (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    materialized_model_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    calibration_result_id TEXT NOT NULL,
                    baseline_snapshot_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_calibration_freezes (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    freeze_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    calibration_result_id TEXT NOT NULL,
                    calibrated_model_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_calibration_holdout_records (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    freeze_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_calibration_evidence_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id TEXT NOT NULL,
                    campaign_sha256 TEXT NOT NULL,
                    consumption_kind TEXT NOT NULL,
                    freeze_id TEXT,
                    record_id TEXT UNIQUE,
                    recorded_at_utc TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_calibration_evidence_campaign
                ON cad_calibration_evidence_events(campaign_id, seq ASC)
                """
            )

    # -- specs ---------------------------------------------------------

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
        payload = self._select(
            'cad_calibration_specs', 'spec_id', spec_id
        )
        if payload is None:
            return None
        return AcousticModelCalibrationSpec.model_validate_json(payload)

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
        payload = self._select(
            'cad_calibration_results', 'result_id', result_id
        )
        if payload is None:
            return None
        return AcousticModelCalibrationResult.model_validate_json(payload)

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
        payload = self._select(
            'cad_calibration_models',
            'materialized_model_id',
            materialized_model_id,
        )
        if payload is None:
            return None
        return CalibratedAcousticModel.model_validate_json(payload)

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
        payload = self._select(
            'cad_calibration_freezes', 'freeze_id', freeze_id
        )
        if payload is None:
            return None
        return CalibratedModelFreeze.model_validate_json(payload)

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
        with closing(self._connect()) as connection:
            row = connection.execute(
                f'SELECT payload_json FROM {table} WHERE {column} = ?',
                (key,),
            ).fetchone()
        return None if row is None else row['payload_json']

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
