"""Persistence for O100C cost records and variant cost evaluations (#514)."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from .cad_installation_cost import CostRecord, VariantCostEvaluation
from .cad_repository import SceneRepository
from .cad_schema import ensure_native_schema, require_native_tables


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CadInstallationCostRepository:
    """Append-only store with replay validation on every read and write."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = Path(scene_repository.path)
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            require_native_tables(
                connection,
                'cad_cost_records',
                'cad_cost_evaluations',
            )


    def save_record(
        self,
        record: CostRecord,
        *,
        document_id: str,
    ) -> CostRecord:
        record = CostRecord.model_validate(record.model_dump(mode='python'))
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT payload_json FROM cad_cost_records WHERE record_id=?',
                (record.record_id,),
            ).fetchone()
            if existing is not None:
                persisted = CostRecord.model_validate_json(
                    existing['payload_json']
                )
                if persisted != record:
                    raise ValueError('cost record id has different semantics')
                return persisted
            connection.execute(
                """
                INSERT INTO cad_cost_records(
                    record_id, record_sha256, document_id, category,
                    payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.record_sha256,
                    document_id,
                    record.category,
                    record.model_dump_json(),
                    _utc_now(),
                ),
            )
        return record

    def get_record(self, record_id: str) -> CostRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_cost_records WHERE record_id=?',
                (record_id,),
            ).fetchone()
        if row is None:
            return None
        return CostRecord.model_validate_json(row['payload_json'])

    def list_records(self, document_id: str) -> tuple[CostRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_cost_records '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CostRecord.model_validate_json(row['payload_json']) for row in rows
        )

    def save_evaluation(
        self,
        evaluation: VariantCostEvaluation,
    ) -> VariantCostEvaluation:
        evaluation = VariantCostEvaluation.model_validate(
            evaluation.model_dump(mode='python')
        )
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT payload_json FROM cad_cost_evaluations '
                'WHERE evaluation_id=?',
                (evaluation.evaluation_id,),
            ).fetchone()
            if existing is not None:
                persisted = VariantCostEvaluation.model_validate_json(
                    existing['payload_json']
                )
                if persisted != evaluation:
                    raise ValueError(
                        'cost evaluation id has different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_cost_evaluations(
                    evaluation_id, evaluation_sha256, document_id, variant_id,
                    payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.evaluation_sha256,
                    evaluation.document_id,
                    evaluation.variant_id,
                    evaluation.model_dump_json(),
                    _utc_now(),
                ),
            )
        return evaluation

    def get_evaluation(
        self,
        evaluation_id: str,
    ) -> VariantCostEvaluation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_cost_evaluations '
                'WHERE evaluation_id=?',
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        return VariantCostEvaluation.model_validate_json(row['payload_json'])

    def list_evaluations(
        self,
        document_id: str,
    ) -> tuple[VariantCostEvaluation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_cost_evaluations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            VariantCostEvaluation.model_validate_json(row['payload_json'])
            for row in rows
        )
