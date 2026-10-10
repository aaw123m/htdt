"""Append-only persistence for uncertain input sets and robust design
assessments (#604).

Persisted rows are re-validated on read — the sealed semantic hash and the
indexed identity columns must agree with the payload, so a corrupted or
tampered row fails closed instead of silently presenting a robust-design
claim the sealed evidence no longer supports.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from ...cad_repository import SceneRepository
from ...cad_schema import connect_sqlite, require_native_tables
from ...cad_uncertainty_propagation import (
    RobustDesignAssessment,
    UncertainInputSet,
)
from ...clock import utc_now_iso as _utc_now


class RobustDesignConflictError(ValueError):
    """An input set/assessment was saved twice with different content."""


class CadRobustDesignRepository:
    """Durable store for #604 input sets and robust design assessments."""

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
                'cad_uncertain_input_sets',
                'cad_robust_design_assessments',
            )

    # -- uncertain input sets --------------------------------------------------

    def save_input_set(self, input_set: UncertainInputSet) -> None:
        existing = self._select_row(
            'cad_uncertain_input_sets', 'input_set_id', input_set.input_set_id
        )
        payload = input_set.model_dump_json()
        if existing is not None:
            if existing['payload_json'] != payload:
                raise RobustDesignConflictError(
                    f'input_set_id {input_set.input_set_id} is persisted '
                    'with different content'
                )
            return
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_uncertain_input_sets ('
                'input_set_id, semantic_sha256, document_id, '
                'scene_revision_id, scene_content_hash, model_ref, '
                'payload_json, recorded_at_utc'
                ') VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    input_set.input_set_id,
                    input_set.semantic_sha256,
                    input_set.document_id,
                    input_set.scene_revision_id,
                    input_set.scene_content_hash,
                    input_set.model_ref,
                    payload,
                    _utc_now(),
                ),
            )

    def get_input_set(
        self, input_set_id: str
    ) -> UncertainInputSet | None:
        row = self._select_row(
            'cad_uncertain_input_sets', 'input_set_id', input_set_id
        )
        if row is None:
            return None
        return self._row_to_input_set(row)

    def list_input_sets_for_document(
        self, document_id: str
    ) -> tuple[UncertainInputSet, ...]:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                'SELECT * FROM cad_uncertain_input_sets '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(self._row_to_input_set(row) for row in rows)

    def _row_to_input_set(self, row: sqlite3.Row) -> UncertainInputSet:
        input_set = UncertainInputSet.model_validate_json(row['payload_json'])
        if (
            row['input_set_id'] != input_set.input_set_id
            or row['semantic_sha256'] != input_set.semantic_sha256
            or row['document_id'] != input_set.document_id
            or row['scene_revision_id'] != input_set.scene_revision_id
            or row['scene_content_hash'] != input_set.scene_content_hash
            or row['model_ref'] != input_set.model_ref
        ):
            raise ValueError(
                'persisted uncertain input set row disagrees with its payload'
            )
        return input_set

    # -- robust design assessments ----------------------------------------------

    def save_assessment(self, assessment: RobustDesignAssessment) -> None:
        existing = self._select_row(
            'cad_robust_design_assessments',
            'assessment_id',
            assessment.assessment_id,
        )
        payload = assessment.model_dump_json()
        if existing is not None:
            if existing['payload_json'] != payload:
                raise RobustDesignConflictError(
                    f'assessment_id {assessment.assessment_id} is persisted '
                    'with different content'
                )
            return
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_robust_design_assessments ('
                'assessment_id, semantic_sha256, document_id, '
                'input_set_id, input_set_sha256, propagation_spec_id, '
                'propagation_spec_sha256, payload_json, recorded_at_utc'
                ') VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    assessment.assessment_id,
                    assessment.semantic_sha256,
                    assessment.document_id,
                    assessment.input_set_id,
                    assessment.input_set_sha256,
                    assessment.propagation_spec_id,
                    assessment.propagation_spec_sha256,
                    payload,
                    _utc_now(),
                ),
            )

    def get_assessment(
        self, assessment_id: str
    ) -> RobustDesignAssessment | None:
        row = self._select_row(
            'cad_robust_design_assessments',
            'assessment_id',
            assessment_id,
        )
        if row is None:
            return None
        return self._row_to_assessment(row)

    def list_assessments_for_document(
        self, document_id: str
    ) -> tuple[RobustDesignAssessment, ...]:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                'SELECT * FROM cad_robust_design_assessments '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(self._row_to_assessment(row) for row in rows)

    def list_assessments_for_input_set(
        self, input_set_id: str
    ) -> tuple[RobustDesignAssessment, ...]:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                'SELECT * FROM cad_robust_design_assessments '
                'WHERE input_set_id=? ORDER BY seq ASC',
                (input_set_id,),
            ).fetchall()
        return tuple(self._row_to_assessment(row) for row in rows)

    def _row_to_assessment(
        self, row: sqlite3.Row
    ) -> RobustDesignAssessment:
        assessment = RobustDesignAssessment.model_validate_json(
            row['payload_json']
        )
        if (
            row['assessment_id'] != assessment.assessment_id
            or row['semantic_sha256'] != assessment.semantic_sha256
            or row['document_id'] != assessment.document_id
            or row['input_set_id'] != assessment.input_set_id
            or row['input_set_sha256'] != assessment.input_set_sha256
            or row['propagation_spec_id'] != assessment.propagation_spec_id
            or row['propagation_spec_sha256'] != assessment.propagation_spec_sha256
        ):
            raise ValueError(
                'persisted robust design assessment row disagrees '
                'with its payload'
            )
        return assessment

    # -- shared ---------------------------------------------------------------

    def _select_row(
        self, table: str, key_column: str, key: str
    ) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                f'SELECT * FROM {table} WHERE {key_column}=?',
                (key,),
            ).fetchone()


__all__ = [
    'CadRobustDesignRepository',
    'RobustDesignConflictError',
]
