"""Persistence for O531 Prediction Matrix authorities (#986).

Specs, execution runs, and per-spec result sets are persisted authorities:
append-only rows with semantic dedupe and fail-closed replay validation.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from .cad_prediction_matrix import (
    MatrixExecutionRun,
    PredictionMatrixSpec,
    TransferMatrixResultSet,
)
from .cad_repository import SceneRepository
from .cad_schema import ensure_native_schema, require_native_tables


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CadPredictionMatrixRepository:
    """Append-only matrix authority store; every read replays validation."""

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
                'cad_prediction_matrix_specs',
                'cad_prediction_matrix_result_sets',
                'cad_prediction_matrix_runs',
            )

    # ------------------------------------------------------------------
    # Specs
    # ------------------------------------------------------------------

    def save_spec(self, spec: PredictionMatrixSpec) -> PredictionMatrixSpec:
        spec = PredictionMatrixSpec.model_validate(
            spec.model_dump(mode='python')
        )
        if self.scene_repository.get(spec.scene_revision_id) is None:
            raise ValueError(
                f'scene revision {spec.scene_revision_id} is not persisted'
            )
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json FROM cad_prediction_matrix_specs
                WHERE spec_id=?
                """,
                (spec.spec_id,),
            ).fetchone()
            if existing is not None:
                persisted = PredictionMatrixSpec.model_validate_json(
                    existing['payload_json']
                )
                if persisted != spec:
                    raise ValueError(
                        'prediction matrix spec id has different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_prediction_matrix_specs(
                    spec_id, semantic_sha256, document_id,
                    scene_revision_id, payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    spec.spec_id,
                    spec.semantic_sha256,
                    spec.document_id,
                    spec.scene_revision_id,
                    spec.model_dump_json(),
                    _utc_now(),
                ),
            )
        return spec

    def get_spec(self, spec_id: str) -> PredictionMatrixSpec | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_prediction_matrix_specs
                WHERE spec_id=?
                """,
                (spec_id,),
            ).fetchone()
        if row is None:
            return None
        return PredictionMatrixSpec.model_validate_json(row['payload_json'])

    def list_specs(
        self, document_id: str
    ) -> tuple[PredictionMatrixSpec, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM cad_prediction_matrix_specs
                WHERE document_id=? ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            PredictionMatrixSpec.model_validate_json(row['payload_json'])
            for row in rows
        )

    def latest_spec(
        self, document_id: str
    ) -> PredictionMatrixSpec | None:
        specs = self.list_specs(document_id)
        return specs[-1] if specs else None

    # ------------------------------------------------------------------
    # Result sets
    # ------------------------------------------------------------------

    def save_result_set(
        self, result_set: TransferMatrixResultSet
    ) -> TransferMatrixResultSet:
        result_set = TransferMatrixResultSet.model_validate(
            result_set.model_dump(mode='python')
        )
        spec = self.get_spec(result_set.spec_id)
        if spec is None:
            raise ValueError(
                'matrix result set has no persisted spec authority: '
                f'{result_set.spec_id}'
            )
        if spec.semantic_sha256 != result_set.spec_semantic_sha256:
            raise ValueError(
                'matrix result set spec hash does not match the persisted spec'
            )
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json FROM cad_prediction_matrix_result_sets
                WHERE result_id=?
                """,
                (result_set.result_id,),
            ).fetchone()
            if existing is not None:
                persisted = TransferMatrixResultSet.model_validate_json(
                    existing['payload_json']
                )
                if persisted != result_set:
                    raise ValueError(
                        'matrix result set id has different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_prediction_matrix_result_sets(
                    result_id, semantic_sha256, spec_id,
                    spec_semantic_sha256, document_id, payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result_set.result_id,
                    result_set.semantic_sha256,
                    result_set.spec_id,
                    result_set.spec_semantic_sha256,
                    spec.document_id,
                    result_set.model_dump_json(),
                    _utc_now(),
                ),
            )
        return result_set

    def get_result_set(
        self, result_id: str
    ) -> TransferMatrixResultSet | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_prediction_matrix_result_sets
                WHERE result_id=?
                """,
                (result_id,),
            ).fetchone()
        if row is None:
            return None
        return TransferMatrixResultSet.model_validate_json(
            row['payload_json']
        )

    def list_result_sets(
        self, spec_id: str
    ) -> tuple[TransferMatrixResultSet, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM cad_prediction_matrix_result_sets
                WHERE spec_id=? ORDER BY seq ASC
                """,
                (spec_id,),
            ).fetchall()
        return tuple(
            TransferMatrixResultSet.model_validate_json(row['payload_json'])
            for row in rows
        )

    def latest_result_set(
        self, spec_id: str
    ) -> TransferMatrixResultSet | None:
        results = self.list_result_sets(spec_id)
        return results[-1] if results else None

    # ------------------------------------------------------------------
    # Execution runs
    # ------------------------------------------------------------------

    def save_run(self, run: MatrixExecutionRun) -> MatrixExecutionRun:
        run = MatrixExecutionRun.model_validate(
            run.model_dump(mode='python')
        )
        spec = self.get_spec(run.spec_id)
        if spec is None:
            raise ValueError(
                'matrix run has no persisted spec authority: '
                f'{run.spec_id}'
            )
        if spec.semantic_sha256 != run.spec_semantic_sha256:
            raise ValueError(
                'matrix run spec hash does not match the persisted spec'
            )
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json FROM cad_prediction_matrix_runs
                WHERE run_id=?
                """,
                (run.run_id,),
            ).fetchone()
            if existing is not None:
                persisted = MatrixExecutionRun.model_validate_json(
                    existing['payload_json']
                )
                if persisted != run:
                    raise ValueError(
                        'matrix run id has different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_prediction_matrix_runs(
                    run_id, semantic_sha256, spec_id, attempt, state,
                    payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.semantic_sha256,
                    run.spec_id,
                    run.attempt,
                    run.state,
                    run.model_dump_json(),
                    _utc_now(),
                ),
            )
        return run

    def get_run(self, run_id: str) -> MatrixExecutionRun | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_prediction_matrix_runs
                WHERE run_id=?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return MatrixExecutionRun.model_validate_json(row['payload_json'])

    def run_history(
        self, spec_id: str
    ) -> tuple[MatrixExecutionRun, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM cad_prediction_matrix_runs
                WHERE spec_id=? ORDER BY seq ASC
                """,
                (spec_id,),
            ).fetchall()
        return tuple(
            MatrixExecutionRun.model_validate_json(row['payload_json'])
            for row in rows
        )
