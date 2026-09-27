"""Append-only persistence for the data acquisition registry (#779).

Six immutable tables: source descriptors, raw-preservation records,
importer declarations, review-status decisions, dataset review records
and upstream version candidates. Status evolves only by appending
``SourceReviewDecision`` rows — the registry's current status is always
resolvable from history, never by rewriting a row.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_data_source_registry import (
    DatasetReviewRecord,
    DataSourceRegistryEntry,
    ImporterDeclaration,
    RawSourceRecord,
    SourceReviewDecision,
    SourceReviewStatus,
    UpstreamVersionCandidate,
)
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite
from .clock import utc_now_iso as _utc_now


class DataSourceRegistryError(ValueError):
    """A registry save/read violated append-only identity rules."""


class CadDataSourceRepository:
    """Native storage for reference-data acquisition authorities (#779)."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            require_native_tables(
                connection,
                'cad_data_source_registry',
                'cad_raw_source_records',
                'cad_importer_declarations',
                'cad_source_review_decisions',
                'cad_dataset_reviews',
                'cad_upstream_version_candidates',
            )

    # -- source registry -----------------------------------------------------

    def save_source(self, entry: DataSourceRegistryEntry) -> None:
        if self.get_source(entry.source_id) is not None:
            raise DataSourceRegistryError(
                'source registry entries are append-only'
            )
        if self.find_source_by_sha256(entry.source_sha256) is not None:
            raise DataSourceRegistryError(
                'source descriptor semantic hash must be unique'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_data_source_registry (
                    source_id, domain, source_sha256, created_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    entry.source_id,
                    entry.domain,
                    entry.source_sha256,
                    _utc_now(),
                    entry.model_dump_json(),
                ),
            )

    def get_source(self, source_id: str) -> DataSourceRegistryEntry | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_data_source_registry '
                'WHERE source_id=?',
                (source_id,),
            ).fetchone()
        if row is None:
            return None
        return DataSourceRegistryEntry.model_validate_json(row['payload_json'])

    def find_source_by_sha256(
        self, source_sha256: str
    ) -> DataSourceRegistryEntry | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_data_source_registry '
                'WHERE source_sha256=?',
                (source_sha256,),
            ).fetchone()
        if row is None:
            return None
        return DataSourceRegistryEntry.model_validate_json(row['payload_json'])

    def list_sources(
        self, domain: str | None = None
    ) -> tuple[DataSourceRegistryEntry, ...]:
        with closing(self._connect()) as connection:
            if domain is None:
                rows = connection.execute(
                    'SELECT payload_json FROM cad_data_source_registry '
                    'ORDER BY created_at_utc, source_id'
                ).fetchall()
            else:
                rows = connection.execute(
                    'SELECT payload_json FROM cad_data_source_registry '
                    'WHERE domain=? ORDER BY created_at_utc, source_id',
                    (domain,),
                ).fetchall()
        return tuple(
            DataSourceRegistryEntry.model_validate_json(row['payload_json'])
            for row in rows
        )

    # -- raw preservation -----------------------------------------------------

    def save_raw_record(self, record: RawSourceRecord) -> None:
        if self.get_source(record.source_id) is None:
            raise DataSourceRegistryError(
                'raw record references an unregistered source'
            )
        if self.get_raw_record(record.record_id) is not None:
            raise DataSourceRegistryError('raw records are append-only')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_raw_source_records (
                    record_id, source_id, record_sha256, created_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.source_id,
                    record.record_sha256,
                    _utc_now(),
                    record.model_dump_json(),
                ),
            )

    def get_raw_record(self, record_id: str) -> RawSourceRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_raw_source_records '
                'WHERE record_id=?',
                (record_id,),
            ).fetchone()
        if row is None:
            return None
        return RawSourceRecord.model_validate_json(row['payload_json'])

    def list_raw_records(self, source_id: str) -> tuple[RawSourceRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_raw_source_records '
                'WHERE source_id=? ORDER BY created_at_utc, record_id',
                (source_id,),
            ).fetchall()
        return tuple(
            RawSourceRecord.model_validate_json(row['payload_json'])
            for row in rows
        )

    # -- importer declarations -------------------------------------------------

    def save_importer(self, declaration: ImporterDeclaration) -> None:
        if self.get_importer(declaration.importer_id) is not None:
            raise DataSourceRegistryError(
                'importer declarations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_importer_declarations (
                    importer_id, domain, importer_sha256, created_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    declaration.importer_id,
                    declaration.domain,
                    declaration.importer_sha256,
                    _utc_now(),
                    declaration.model_dump_json(),
                ),
            )

    def get_importer(self, importer_id: str) -> ImporterDeclaration | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_importer_declarations '
                'WHERE importer_id=?',
                (importer_id,),
            ).fetchone()
        if row is None:
            return None
        return ImporterDeclaration.model_validate_json(row['payload_json'])

    def list_importers(
        self, domain: str | None = None
    ) -> tuple[ImporterDeclaration, ...]:
        with closing(self._connect()) as connection:
            if domain is None:
                rows = connection.execute(
                    'SELECT payload_json FROM cad_importer_declarations '
                    'ORDER BY created_at_utc, importer_id'
                ).fetchall()
            else:
                rows = connection.execute(
                    'SELECT payload_json FROM cad_importer_declarations '
                    'WHERE domain=? ORDER BY created_at_utc, importer_id',
                    (domain,),
                ).fetchall()
        return tuple(
            ImporterDeclaration.model_validate_json(row['payload_json'])
            for row in rows
        )

    # -- review-status decisions -------------------------------------------------

    def save_decision(self, decision: SourceReviewDecision) -> None:
        current = self.current_review_status(decision.source_id)
        if current is None:
            raise DataSourceRegistryError(
                'review decision references an unregistered source'
            )
        if decision.from_status != current:
            raise DataSourceRegistryError(
                'review decision from_status does not match the current '
                'registry status'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_source_review_decisions (
                    decision_id, source_id, decision_sha256, reviewed_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    decision.source_id,
                    decision.decision_sha256,
                    decision.reviewed_at_utc,
                    decision.model_dump_json(),
                ),
            )

    def get_decision(self, decision_id: str) -> SourceReviewDecision | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_source_review_decisions '
                'WHERE decision_id=?',
                (decision_id,),
            ).fetchone()
        if row is None:
            return None
        return SourceReviewDecision.model_validate_json(row['payload_json'])

    def list_decisions(
        self, source_id: str
    ) -> tuple[SourceReviewDecision, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_source_review_decisions '
                'WHERE source_id=? ORDER BY reviewed_at_utc, decision_id',
                (source_id,),
            ).fetchall()
        return tuple(
            SourceReviewDecision.model_validate_json(row['payload_json'])
            for row in rows
        )

    def current_review_status(
        self, source_id: str
    ) -> SourceReviewStatus | None:
        """Latest decision's ``to_status``, else the descriptor's initial.

        ``None`` when the source is unregistered.
        """

        entry = self.get_source(source_id)
        if entry is None:
            return None
        decisions = self.list_decisions(source_id)
        if not decisions:
            return entry.initial_review_status
        return decisions[-1].to_status

    # -- dataset review records -------------------------------------------------

    def save_dataset_review(self, review: DatasetReviewRecord) -> None:
        if self.get_source(review.source_id) is None:
            raise DataSourceRegistryError(
                'dataset review references an unregistered source'
            )
        if review.record_id is not None and self.get_raw_record(
            review.record_id
        ) is None:
            raise DataSourceRegistryError(
                'dataset review references an unknown raw record'
            )
        if review.importer_id is not None and self.get_importer(
            review.importer_id
        ) is None:
            raise DataSourceRegistryError(
                'dataset review references an unknown importer'
            )
        if self.get_dataset_review(review.review_id) is not None:
            raise DataSourceRegistryError('dataset reviews are append-only')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_dataset_reviews (
                    review_id, source_id, review_sha256, reviewed_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    review.review_id,
                    review.source_id,
                    review.review_sha256,
                    review.reviewed_at_utc,
                    review.model_dump_json(),
                ),
            )

    def get_dataset_review(self, review_id: str) -> DatasetReviewRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_dataset_reviews WHERE review_id=?',
                (review_id,),
            ).fetchone()
        if row is None:
            return None
        return DatasetReviewRecord.model_validate_json(row['payload_json'])

    def list_dataset_reviews(
        self, source_id: str
    ) -> tuple[DatasetReviewRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_dataset_reviews '
                'WHERE source_id=? ORDER BY reviewed_at_utc, review_id',
                (source_id,),
            ).fetchall()
        return tuple(
            DatasetReviewRecord.model_validate_json(row['payload_json'])
            for row in rows
        )

    # -- upstream version candidates -------------------------------------------------

    def save_upstream_candidate(
        self, candidate: UpstreamVersionCandidate
    ) -> None:
        if self.get_source(candidate.source_id) is None:
            raise DataSourceRegistryError(
                'upstream candidate references an unregistered source'
            )
        if self.get_upstream_candidate(candidate.candidate_id) is not None:
            raise DataSourceRegistryError(
                'upstream candidates are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_upstream_version_candidates (
                    candidate_id, source_id, candidate_sha256, created_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    candidate.candidate_id,
                    candidate.source_id,
                    candidate.candidate_sha256,
                    _utc_now(),
                    candidate.model_dump_json(),
                ),
            )

    def get_upstream_candidate(
        self, candidate_id: str
    ) -> UpstreamVersionCandidate | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_upstream_version_candidates '
                'WHERE candidate_id=?',
                (candidate_id,),
            ).fetchone()
        if row is None:
            return None
        return UpstreamVersionCandidate.model_validate_json(row['payload_json'])

    def list_upstream_candidates(
        self, source_id: str
    ) -> tuple[UpstreamVersionCandidate, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_upstream_version_candidates '
                'WHERE source_id=? ORDER BY created_at_utc, candidate_id',
                (source_id,),
            ).fetchall()
        return tuple(
            UpstreamVersionCandidate.model_validate_json(row['payload_json'])
            for row in rows
        )


__all__ = [
    'CadDataSourceRepository',
    'DataSourceRegistryError',
]
