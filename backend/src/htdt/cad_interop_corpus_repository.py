"""Append-only persistence for the #892 interop corpus authority.

Two tables share the #806 ``_SealedStore`` machinery — save-time seal
re-verification, read-time column-vs-payload checks, append-only
conflict on id+sha divergence:

- ``cad_interop_fixture_runs`` — sealed
  :class:`~htdt.cad_interop_corpus.InteropFixtureRunRecord` verdicts
  (``icr-``) pinned to the exact fixture sha + manifest sha they ran;
- ``cad_interop_corpus_runs`` — sealed
  :class:`~htdt.cad_interop_corpus.InteropCorpusRunRecord` corpus
  verdicts (``icx-``) pinned to the manifest sha.

Manifests themselves are not stored: a corpus manifest is a global,
versioned document (``build_default_corpus_manifest``/external issues),
not document-scoped state — every run record pins the manifest sha it
ran against, so evidence can never drift silently onto a different
corpus revision.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_interop_corpus import (
    InteropCorpusRunRecord,
    InteropFixtureRunRecord,
)
from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .cad_calibration_deployment_repository import _SealedStore


class CadInteropCorpusRepository:
    """Native storage for interop corpus fixture + corpus run records."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_interop_fixture_runs',
                'cad_interop_corpus_runs',
            )
        self.fixture_runs = _SealedStore(
            self._connect,
            'cad_interop_fixture_runs',
            InteropFixtureRunRecord, 'run_id',
            'run_sha256',
            (
                ('document_id', '__document_id__'),
                ('fixture_id', 'fixture_ref.ref_id'),
                ('fixture_sha256', 'fixture_ref.ref_sha256'),
                ('manifest_sha256', 'manifest_sha256'),
                ('corpus_version', 'corpus_version'),
                ('harness_version', 'harness_version'),
                ('format_family', 'format_family'),
                ('round_trip_mode', 'round_trip_mode'),
                ('verdict', 'verdict'),
                ('started_at_utc', 'started_at_utc'),
                ('finished_at_utc', 'finished_at_utc'),
            ),
        )
        self.corpus_runs = _SealedStore(
            self._connect,
            'cad_interop_corpus_runs',
            InteropCorpusRunRecord, 'corpus_run_id',
            'corpus_run_sha256',
            (
                ('document_id', '__document_id__'),
                ('manifest_id', 'manifest_ref.ref_id'),
                ('manifest_sha256', 'manifest_ref.ref_sha256'),
                ('corpus_version', 'corpus_version'),
                ('harness_version', 'harness_version'),
                ('verdict', 'verdict'),
                ('started_at_utc', 'started_at_utc'),
                ('finished_at_utc', 'finished_at_utc'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # -- fixture runs -----------------------------------------------------

    def save_fixture_run(self, run: InteropFixtureRunRecord) -> None:
        self.fixture_runs.save(run)

    def get_fixture_run(
        self, run_id: str,
    ) -> InteropFixtureRunRecord | None:
        return self.fixture_runs.get(run_id)

    def list_fixture_runs(
        self, document_id: str | None = None,
    ) -> tuple[InteropFixtureRunRecord, ...]:
        return self.fixture_runs.list(document_id)

    # -- corpus runs ------------------------------------------------------

    def save_corpus_run(self, run: InteropCorpusRunRecord) -> None:
        self.corpus_runs.save(run)

    def get_corpus_run(
        self, corpus_run_id: str,
    ) -> InteropCorpusRunRecord | None:
        return self.corpus_runs.get(corpus_run_id)

    def list_corpus_runs(
        self, document_id: str | None = None,
    ) -> tuple[InteropCorpusRunRecord, ...]:
        return self.corpus_runs.list(document_id)


__all__ = ['CadInteropCorpusRepository']
