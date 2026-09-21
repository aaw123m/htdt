from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .cad_prediction_models import (
    CadPredictionResult,
    CadPredictedReflection,
    CadPredictedRoomMode,
    canonical_prediction_json,
    prediction_input_hash,
)
from .cad_prediction_request import verify_prediction_input
from .cad_repository import SceneRepository, SceneRevision


class CadPredictionRepository:
    """Immutable native prediction storage bound directly to SceneRevision rows."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = Path(scene_repository.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS cad_prediction_results (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    prediction_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    scene_revision_id TEXT NOT NULL,
                    scene_content_hash TEXT NOT NULL,
                    constraint_workspace_hash TEXT,
                    model_id TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    result_kind TEXT NOT NULL,
                    geometry_compatibility TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    input_snapshot_json TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    submitted_at_utc TEXT NOT NULL,
                    completed_at_utc TEXT NOT NULL,
                    status TEXT NOT NULL,
                    assumptions_json TEXT NOT NULL,
                    warnings_json TEXT NOT NULL,
                    modes_json TEXT NOT NULL,
                    reflections_json TEXT NOT NULL,
                    FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id)
                )
                '''
            )
            connection.execute(
                'CREATE INDEX IF NOT EXISTS idx_prediction_document_seq '
                'ON cad_prediction_results(document_id, seq DESC)'
            )
            connection.execute(
                'CREATE INDEX IF NOT EXISTS idx_prediction_run_seq '
                'ON cad_prediction_results(run_id, seq ASC)'
            )

    def _source_revision(self, result: CadPredictionResult) -> SceneRevision:
        source = self.scene_repository.get(result.scene_revision_id)
        if source is None:
            raise ValueError('prediction source revision does not exist')
        if source.document_id != result.document_id:
            raise ValueError('prediction source revision belongs to another document')
        if source.content_hash != result.scene_content_hash:
            raise ValueError('prediction source content hash does not match revision')
        return source

    @staticmethod
    def _require_canonical_input(source: SceneRevision, result: CadPredictionResult) -> None:
        verify_prediction_input(
            source,
            model_id=result.model_id,
            model_version=result.model_version,
            parameters_json=result.parameters_json,
            input_snapshot_json=result.input_snapshot_json,
            input_hash=result.input_hash,
            geometry_compatibility=result.geometry_compatibility,
        )

    def save(self, result: CadPredictionResult) -> None:
        source = self._source_revision(result)
        if prediction_input_hash(result.input_snapshot_json) != result.input_hash:
            raise ValueError('prediction input hash mismatch')
        self._require_canonical_input(source, result)

        with closing(self._connect()) as connection, connection:
            connection.execute(
                '''
                INSERT INTO cad_prediction_results(
                    prediction_id, run_id, document_id, scene_revision_id, scene_content_hash,
                    constraint_workspace_hash, model_id, model_version, result_kind,
                    geometry_compatibility, parameters_json, input_snapshot_json, input_hash,
                    submitted_at_utc, completed_at_utc, status, assumptions_json,
                    warnings_json, modes_json, reflections_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    result.prediction_id,
                    result.run_id,
                    result.document_id,
                    result.scene_revision_id,
                    result.scene_content_hash,
                    result.constraint_workspace_hash,
                    result.model_id,
                    result.model_version,
                    result.result_kind,
                    result.geometry_compatibility,
                    result.parameters_json,
                    result.input_snapshot_json,
                    result.input_hash,
                    result.submitted_at_utc,
                    result.completed_at_utc,
                    result.status,
                    canonical_prediction_json(list(result.assumptions)),
                    canonical_prediction_json(list(result.warnings)),
                    canonical_prediction_json([mode.model_dump(mode='json') for mode in result.modes]),
                    canonical_prediction_json(
                        [reflection.model_dump(mode='json') for reflection in result.reflections]
                    ),
                ),
            )

    def get(self, prediction_id: str) -> CadPredictionResult | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_prediction_results WHERE prediction_id=?',
                (prediction_id,),
            ).fetchone()
        return None if row is None else self._row_to_result(row)

    def list_results(self, document_id: str) -> tuple[CadPredictionResult, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM cad_prediction_results WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(self._row_to_result(row) for row in rows)

    def list_run(self, run_id: str) -> tuple[CadPredictionResult, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM cad_prediction_results WHERE run_id=? ORDER BY seq ASC',
                (run_id,),
            ).fetchall()
        return tuple(self._row_to_result(row) for row in rows)

    def _row_to_result(self, row: sqlite3.Row) -> CadPredictionResult:
        modes = tuple(CadPredictedRoomMode.model_validate(item) for item in json.loads(row['modes_json']))
        reflections = tuple(
            CadPredictedReflection.model_validate(item) for item in json.loads(row['reflections_json'])
        )
        result = CadPredictionResult(
            prediction_id=row['prediction_id'],
            run_id=row['run_id'],
            document_id=row['document_id'],
            scene_revision_id=row['scene_revision_id'],
            scene_content_hash=row['scene_content_hash'],
            constraint_workspace_hash=row['constraint_workspace_hash'],
            model_id=row['model_id'],
            model_version=row['model_version'],
            result_kind=row['result_kind'],
            geometry_compatibility=row['geometry_compatibility'],
            parameters_json=row['parameters_json'],
            input_snapshot_json=row['input_snapshot_json'],
            input_hash=row['input_hash'],
            submitted_at_utc=row['submitted_at_utc'],
            completed_at_utc=row['completed_at_utc'],
            status=row['status'],
            assumptions=tuple(json.loads(row['assumptions_json'])),
            warnings=tuple(json.loads(row['warnings_json'])),
            modes=modes,
            reflections=reflections,
        )
        # Reads are authoritative: a stored row must still replay to the
        # canonical model input of its exact source SceneRevision, so a
        # coherently rewritten row cannot survive by recomputing hashes.
        self._require_canonical_input(self._source_revision(result), result)
        return result


def prediction_timestamp_utc() -> str:
    return datetime.now(timezone.utc).isoformat()
