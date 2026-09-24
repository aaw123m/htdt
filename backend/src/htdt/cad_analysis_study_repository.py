"""Append-only persistence for saved analysis studies (#594)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_analysis_study import AnalysisStudy
from .cad_repository import SceneRepository


class AnalysisStudyConflictError(ValueError):
    """A study save violated append-only identity rules."""


class CadAnalysisStudyRepository:
    """Native storage for AnalysisStudy records.

    Studies are immutable: the only "update" path is duplicating a study into
    a new record (``duplicated_from_study_id`` / ``supersedes_study_id`` keep
    the iteration chain auditable).
    """

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
                CREATE TABLE IF NOT EXISTS cad_analysis_studies (
                    study_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    study_kind TEXT NOT NULL,
                    study_sha256 TEXT NOT NULL,
                    supersedes_study_id TEXT,
                    duplicated_from_study_id TEXT,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cad_analysis_studies_document
                ON cad_analysis_studies (document_id, created_at_utc)
                """
            )

    def save_study(self, study: AnalysisStudy) -> None:
        if self.get_study(study.study_id) is not None:
            raise AnalysisStudyConflictError(
                'AnalysisStudy ids are append-only'
            )
        for prior_id in (
            study.supersedes_study_id,
            study.duplicated_from_study_id,
        ):
            if prior_id is not None:
                prior = self.get_study(prior_id)
                if prior is None:
                    raise ValueError('chained study is not persisted')
                if prior.document_id != study.document_id:
                    raise ValueError('chained study belongs to another document')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_analysis_studies (
                    study_id, document_id, study_kind, study_sha256,
                    supersedes_study_id, duplicated_from_study_id,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    study.study_id,
                    study.document_id,
                    study.study_kind,
                    study.study_sha256,
                    study.supersedes_study_id,
                    study.duplicated_from_study_id,
                    study.created_at_utc,
                    study.model_dump_json(),
                ),
            )

    def get_study(self, study_id: str) -> AnalysisStudy | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_analysis_studies WHERE study_id=?',
                (study_id,),
            ).fetchone()
        if row is None:
            return None
        return AnalysisStudy.model_validate_json(row['payload_json'])

    def list_studies(
        self,
        document_id: str,
    ) -> tuple[AnalysisStudy, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_analysis_studies
                WHERE document_id=?
                ORDER BY created_at_utc, study_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            AnalysisStudy.model_validate_json(row['payload_json'])
            for row in rows
        )


__all__ = ['AnalysisStudyConflictError', 'CadAnalysisStudyRepository']
