"""Append-only persistence for the validation corpus (#773).

Two immutable tables: corpus-entry manifests and predeclared benchmark
specs. No row is ever updated or deleted — a corpus can only grow, and
every evaluation run binds the exact spec persisted before it ran, so
results are never retrofitted onto a changed contract.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import require_native_tables
from .cad_validation_corpus import (
    ValidationBenchmarkSpec,
    ValidationCorpusEntry,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ValidationCorpusError(ValueError):
    """A corpus save/list/read violated append-only identity rules."""


class CadValidationCorpusRepository:
    """Native storage for validation-corpus manifests and benchmark specs."""

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
        with closing(self._connect()) as connection:
            require_native_tables(
                connection,
                'cad_validation_corpus_entries',
                'cad_validation_benchmark_specs',
            )

    # -- corpus entries ----------------------------------------------------

    def save_entry(self, entry: ValidationCorpusEntry) -> None:
        if self.get_entry(entry.corpus_entry_id) is not None:
            raise ValidationCorpusError('corpus entries are append-only')
        if self.find_entry_by_sha256(entry.corpus_entry_sha256) is not None:
            raise ValidationCorpusError(
                'corpus entry semantic hash must be unique'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_validation_corpus_entries (
                    corpus_entry_id, document_id, corpus_entry_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    entry.corpus_entry_id,
                    entry.document_id,
                    entry.corpus_entry_sha256,
                    _utc_now(),
                    entry.model_dump_json(),
                ),
            )

    def get_entry(self, corpus_entry_id: str) -> ValidationCorpusEntry | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_validation_corpus_entries '
                'WHERE corpus_entry_id=?',
                (corpus_entry_id,),
            ).fetchone()
        if row is None:
            return None
        return ValidationCorpusEntry.model_validate_json(row['payload_json'])

    def find_entry_by_sha256(
        self, corpus_entry_sha256: str
    ) -> ValidationCorpusEntry | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_validation_corpus_entries '
                'WHERE corpus_entry_sha256=?',
                (corpus_entry_sha256,),
            ).fetchone()
        if row is None:
            return None
        return ValidationCorpusEntry.model_validate_json(row['payload_json'])

    def list_entries(
        self, document_id: str | None = None
    ) -> tuple[ValidationCorpusEntry, ...]:
        """Entries of one project, or the whole corpus when unscoped (#773).

        The corpus intentionally spans projects — pass ``None`` to audit
        cross-project coverage; pass a document id for project views.
        """
        with closing(self._connect()) as connection:
            if document_id is None:
                rows = connection.execute(
                    'SELECT payload_json FROM cad_validation_corpus_entries '
                    'ORDER BY created_at_utc, corpus_entry_id'
                ).fetchall()
            else:
                rows = connection.execute(
                    'SELECT payload_json FROM cad_validation_corpus_entries '
                    'WHERE document_id=? ORDER BY created_at_utc, corpus_entry_id',
                    (document_id,),
                ).fetchall()
        return tuple(
            ValidationCorpusEntry.model_validate_json(row['payload_json'])
            for row in rows
        )

    # -- benchmark specs ---------------------------------------------------

    def save_benchmark_spec(self, spec: ValidationBenchmarkSpec) -> None:
        if self.get_benchmark_spec(spec.benchmark_spec_id) is not None:
            raise ValidationCorpusError('benchmark specs are append-only')
        if (
            self.find_benchmark_spec_by_sha256(spec.benchmark_spec_sha256)
            is not None
        ):
            raise ValidationCorpusError(
                'benchmark spec semantic hash must be unique'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_validation_benchmark_specs (
                    benchmark_spec_id, benchmark_spec_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    spec.benchmark_spec_id,
                    spec.benchmark_spec_sha256,
                    _utc_now(),
                    spec.model_dump_json(),
                ),
            )

    def get_benchmark_spec(
        self, benchmark_spec_id: str
    ) -> ValidationBenchmarkSpec | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_validation_benchmark_specs '
                'WHERE benchmark_spec_id=?',
                (benchmark_spec_id,),
            ).fetchone()
        if row is None:
            return None
        return ValidationBenchmarkSpec.model_validate_json(row['payload_json'])

    def find_benchmark_spec_by_sha256(
        self, benchmark_spec_sha256: str
    ) -> ValidationBenchmarkSpec | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_validation_benchmark_specs '
                'WHERE benchmark_spec_sha256=?',
                (benchmark_spec_sha256,),
            ).fetchone()
        if row is None:
            return None
        return ValidationBenchmarkSpec.model_validate_json(row['payload_json'])

    def list_benchmark_specs(self) -> tuple[ValidationBenchmarkSpec, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_validation_benchmark_specs '
                'ORDER BY created_at_utc, benchmark_spec_id'
            ).fetchall()
        return tuple(
            ValidationBenchmarkSpec.model_validate_json(row['payload_json'])
            for row in rows
        )


__all__ = [
    'CadValidationCorpusRepository',
    'ValidationCorpusError',
]
