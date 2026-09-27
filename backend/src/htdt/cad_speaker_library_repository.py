"""Append-only persistence for the curated speaker library (#772)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite
from .cad_speaker_library import (
    BUILTIN_SPEAKER_LIBRARY,
    SpeakerDataset,
    SpeakerDefinition,
)


class SpeakerLibraryConflictError(ValueError):
    """A speaker/dataset save violated append-only identity rules."""


class CadSpeakerLibraryRepository:
    """Native storage for SpeakerDefinition + SpeakerDataset.

    Definitions are append-only per ``speaker_id``; datasets are
    append-only per ``(speaker_id, version, kind)``.
    """

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
            require_native_tables(connection, 'cad_speaker_definitions', 'cad_speaker_datasets')

    def save_speaker(self, speaker: SpeakerDefinition) -> None:
        if self.get_speaker(speaker.speaker_id) is not None:
            raise SpeakerLibraryConflictError(
                'SpeakerDefinition ids are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_speaker_definitions (
                    speaker_id, document_id, speaker_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    speaker.speaker_id,
                    speaker.document_id,
                    speaker.speaker_sha256,
                    speaker.created_at_utc,
                    speaker.model_dump_json(),
                ),
            )

    def get_speaker(self, speaker_id: str) -> SpeakerDefinition | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_speaker_definitions WHERE speaker_id=?',
                (speaker_id,),
            ).fetchone()
        if row is None:
            return None
        return SpeakerDefinition.model_validate_json(row['payload_json'])

    def list_speakers(
        self, document_id: str | None = None
    ) -> tuple[SpeakerDefinition, ...]:
        """Shared/bundled entries plus (optionally) one project's entries."""
        with closing(self._connect()) as connection:
            if document_id is None:
                rows = connection.execute(
                    """
                    SELECT payload_json FROM cad_speaker_definitions
                    WHERE document_id IS NULL ORDER BY created_at_utc, speaker_id
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT payload_json FROM cad_speaker_definitions
                    WHERE document_id IS NULL OR document_id=?
                    ORDER BY created_at_utc, speaker_id
                    """,
                    (document_id,),
                ).fetchall()
        return tuple(
            SpeakerDefinition.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Datasets

    def save_dataset(self, dataset: SpeakerDataset) -> None:
        if self.get_speaker(dataset.speaker_id) is None:
            raise ValueError(
                'dataset references a SpeakerDefinition that is not persisted'
            )
        speaker = self.get_speaker(dataset.speaker_id)
        if (
            speaker is not None
            and speaker.document_id is None
            and not dataset.redistribution_permitted
        ):
            raise ValueError(
                'shared/bundled library datasets must carry explicit '
                'redistribution licensing (redistribution_permitted=True)'
            )
        for existing in self.list_datasets(dataset.speaker_id):
            if (
                existing.version == dataset.version
                and existing.kind == dataset.kind
            ):
                raise SpeakerLibraryConflictError(
                    'SpeakerDataset (speaker_id, version, kind) is append-only'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_speaker_datasets (
                    dataset_id, speaker_id, version, kind,
                    dataset_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dataset.dataset_id,
                    dataset.speaker_id,
                    dataset.version,
                    dataset.kind,
                    dataset.dataset_sha256,
                    dataset.created_at_utc,
                    dataset.model_dump_json(),
                ),
            )

    def get_dataset(self, dataset_id: str) -> SpeakerDataset | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_speaker_datasets WHERE dataset_id=?',
                (dataset_id,),
            ).fetchone()
        if row is None:
            return None
        return SpeakerDataset.model_validate_json(row['payload_json'])

    def get_dataset_by_hash(
        self, dataset_sha256: str
    ) -> SpeakerDataset | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_speaker_datasets WHERE dataset_sha256=?',
                (dataset_sha256,),
            ).fetchone()
        if row is None:
            return None
        return SpeakerDataset.model_validate_json(row['payload_json'])

    def list_datasets(
        self, speaker_id: str, kind: str | None = None
    ) -> tuple[SpeakerDataset, ...]:
        with closing(self._connect()) as connection:
            if kind is None:
                rows = connection.execute(
                    """
                    SELECT payload_json FROM cad_speaker_datasets
                    WHERE speaker_id=? ORDER BY created_at_utc, dataset_id
                    """,
                    (speaker_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT payload_json FROM cad_speaker_datasets
                    WHERE speaker_id=? AND kind=?
                    ORDER BY created_at_utc, dataset_id
                    """,
                    (speaker_id, kind),
                ).fetchall()
        return tuple(
            SpeakerDataset.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Bundled library

    def install_builtin_library(self) -> int:
        """Seed bundled entries idempotently. Returns count newly added."""
        installed = 0
        for speaker, dataset in BUILTIN_SPEAKER_LIBRARY:
            if self.get_speaker(speaker.speaker_id) is None:
                self.save_speaker(speaker)
                installed += 1
            if self.get_dataset(dataset.dataset_id) is None:
                self.save_dataset(dataset)
        return installed
