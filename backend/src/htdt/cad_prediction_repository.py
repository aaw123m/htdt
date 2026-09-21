from __future__ import annotations

from collections.abc import Iterable
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
    prediction_result_sha256,
)
from .cad_prediction_request import verify_prediction_input, verify_prediction_output
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
                    result_sha256 TEXT,
                    FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id)
                )
                '''
            )
            # Output-identity migration: rows written before result_sha256
            # existed keep NULL and are non-authoritative — reads fail closed
            # (``_row_to_result``) rather than silently fabricating a hash for
            # output this version never attested.
            columns = {
                row['name']
                for row in connection.execute('PRAGMA table_info(cad_prediction_results)')
            }
            if 'result_sha256' not in columns:
                connection.execute(
                    'ALTER TABLE cad_prediction_results ADD COLUMN result_sha256 TEXT'
                )
            connection.execute(
                'CREATE INDEX IF NOT EXISTS idx_prediction_document_seq '
                'ON cad_prediction_results(document_id, seq DESC)'
            )
            connection.execute(
                'CREATE INDEX IF NOT EXISTS idx_prediction_run_seq '
                'ON cad_prediction_results(run_id, seq ASC)'
            )
            # One run occupies each (run_id, result_kind) slot exactly once:
            # the run-level write authority enforces it deterministically and
            # this index is the stored backstop for it.
            connection.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS idx_prediction_run_result_kind '
                'ON cad_prediction_results(run_id, result_kind)'
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

    def _validate_result(self, result: CadPredictionResult) -> None:
        """Resolve the exact source revision and replay the canonical input/output.

        This is the single authoritative validation path shared by save and
        read: it binds the record to its exact SceneRevision, replays the
        canonical model input, re-verifies the stored ``result_sha256``
        self-hash (guarding records built through ``model_copy``, which skips
        model validators), and finally replays the pinned model output so a
        coherently rewritten row — payload and self-consistent hash together
        — still fails closed.

        This runs on a second connection through ``SceneRepository.get``, so
        callers must finish it BEFORE opening the write transaction: opening a
        nested connection while BEGIN IMMEDIATE is held can deadlock the write.
        """
        source = self._source_revision(result)
        if prediction_input_hash(result.input_snapshot_json) != result.input_hash:
            raise ValueError('prediction input hash mismatch')
        self._require_canonical_input(source, result)
        if prediction_result_sha256(result.result_identity_payload()) != result.result_sha256:
            raise ValueError('prediction result_sha256 does not match the result identity payload')
        verify_prediction_output(source, result)

    # Fields that every record of one prediction run must share: the run binds
    # one document, one exact SceneRevision, one model identity and one
    # canonical model input.
    _RUN_IDENTITY_FIELDS = (
        'document_id',
        'scene_revision_id',
        'scene_content_hash',
        'constraint_workspace_hash',
        'model_id',
        'model_version',
        'parameters_json',
        'input_snapshot_json',
        'input_hash',
        'geometry_compatibility',
    )

    def save(self, result: CadPredictionResult) -> None:
        """Persist one result through the run authority as a single-record run."""
        self.save_run((result,))

    def save_run(
        self,
        results: Iterable[CadPredictionResult],
    ) -> tuple[CadPredictionResult, ...]:
        """Atomically persist every result record of one prediction run.

        A rectangular prediction run is a tuple of CadPredictionResult records
        sharing one ``run_id`` — ``geometry_modes`` plus
        ``geometry_reflections``. Every record is validated before the write
        transaction begins (``_validate_result`` resolves revisions on a second
        connection, which must not run while BEGIN IMMEDIATE is held), then all
        inserts commit or roll back together: a mid-commit failure can never
        leave a partially persisted run for ``list_run`` to expose.

        The batch contract is enforced deterministically before any write: the
        run must share one ``run_id`` and one document/revision/model/input
        identity, and may not repeat a ``prediction_id`` or ``result_kind``.
        Persisted duplicates are rejected inside the write transaction as well,
        so the ``(run_id, result_kind)`` slot of a committed run cannot be
        occupied twice even by a racing writer.
        """
        items = tuple(results)
        if not items:
            raise ValueError('prediction run requires at least one result')
        if any(not isinstance(item, CadPredictionResult) for item in items):
            raise ValueError('prediction run results must be CadPredictionResult records')
        first = items[0]
        if any(item.run_id != first.run_id for item in items):
            raise ValueError('prediction run results must share one run_id')
        for field in self._RUN_IDENTITY_FIELDS:
            if any(getattr(item, field) != getattr(first, field) for item in items):
                raise ValueError(f'prediction run results disagree on {field}')
        if len({item.prediction_id for item in items}) != len(items):
            raise ValueError('prediction run contains duplicate prediction identities')
        if len({item.result_kind for item in items}) != len(items):
            raise ValueError('prediction run contains duplicate result kinds')
        for item in items:
            self._validate_result(item)

        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            for item in items:
                self._save_result_in_transaction(connection, item)
        return items

    def _save_result_in_transaction(
        self,
        connection: sqlite3.Connection,
        result: CadPredictionResult,
    ) -> None:
        """Insert one validated result inside the caller's transaction.

        The caller owns BEGIN/COMMIT/ROLLBACK and must have validated the
        record first. The duplicate checks run under the held BEGIN IMMEDIATE
        so a racing writer cannot interleave a second row for the same
        prediction identity or the same (run_id, result_kind) slot.
        """
        row = connection.execute(
            'SELECT prediction_id FROM cad_prediction_results WHERE prediction_id=?',
            (result.prediction_id,),
        ).fetchone()
        if row is not None:
            raise ValueError('duplicate prediction result identity')
        row = connection.execute(
            'SELECT prediction_id FROM cad_prediction_results '
            'WHERE run_id=? AND result_kind=?',
            (result.run_id, result.result_kind),
        ).fetchone()
        if row is not None:
            raise ValueError('prediction run already contains this result kind')
        connection.execute(
            '''
            INSERT INTO cad_prediction_results(
                prediction_id, run_id, document_id, scene_revision_id, scene_content_hash,
                constraint_workspace_hash, model_id, model_version, result_kind,
                geometry_compatibility, parameters_json, input_snapshot_json, input_hash,
                submitted_at_utc, completed_at_utc, status, assumptions_json,
                warnings_json, modes_json, reflections_json, result_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                result.result_sha256,
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
        if row['result_sha256'] is None:
            # Backward-compatibility policy: records written before output
            # identity existed never had their result payload attested, so
            # they are non-authoritative and fail closed on read rather than
            # acquiring a fabricated current hash.
            raise ValueError(
                'prediction result predates result_sha256 output identity '
                'and is non-authoritative'
            )
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
            result_sha256=row['result_sha256'],
        )
        # Reads are authoritative: a stored row must still replay to the
        # canonical model input and output of its exact source SceneRevision,
        # so a coherently rewritten row cannot survive by recomputing hashes.
        self._validate_result(result)
        return result


def prediction_timestamp_utc() -> str:
    return datetime.now(timezone.utc).isoformat()
