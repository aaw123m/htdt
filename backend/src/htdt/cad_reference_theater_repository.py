"""Append-only persistence for the #891 Reference Theater authority.

One table — ``cad_reference_theater_runs`` — behind the shared
``_SealedStore`` machinery (save-time seal re-verification, read-time
column-vs-payload checks). Every self-test lane run seals exactly one
record, including failed/blocked/unauthorized/drift runs: a self-test
that left no record is itself an installation-health failure.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .cad_calibration_deployment_repository import _SealedStore
from .cad_reference_theater import CadReferenceTheaterRun


class CadReferenceTheaterRepository:
    """Native storage for the #891 Reference Theater self-test authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_reference_theater_runs')
        self.runs = _SealedStore(
            self._connect,
            'cad_reference_theater_runs',
            CadReferenceTheaterRun,
            'run_id',
            'run_sha256',
            (
                ('document_id', '__document_id__'),
                ('fixture_version', 'fixture_version'),
                ('outcome', 'outcome'),
                ('verdict', 'verdict'),
                ('manifest_sha256', 'manifest_sha256'),
                ('deploy_is_simulated', 'deploy_is_simulated'),
                ('deploy_evidence_strength', 'deploy_evidence_strength'),
                ('scene_sha256', 'scene_sha256'),
                ('started_at_utc', 'started_at_utc'),
                ('finished_at_utc', 'finished_at_utc'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # -- writes ---------------------------------------------------------------

    def save_run(self, record: CadReferenceTheaterRun) -> None:
        self.runs.save(record)

    # -- reads ----------------------------------------------------------------

    def get_run(self, run_id: str) -> CadReferenceTheaterRun | None:
        return self.runs.get(run_id)

    def list_runs(
        self,
        document_id: str | None = None,
        *,
        fixture_version: str | None = None,
    ) -> list[CadReferenceTheaterRun]:
        clause = ''
        params: tuple[str, ...] = ()
        if document_id is not None:
            clause = ' WHERE document_id = ?'
            params = (document_id,)
        if fixture_version is not None:
            clause = (
                ' WHERE fixture_version = ?' if not clause
                else clause + ' AND fixture_version = ?')
            params = params + (fixture_version,)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT run_id FROM cad_reference_theater_runs'
                + clause + ' ORDER BY seq ASC',
                params,
            ).fetchall()
        records: list[CadReferenceTheaterRun] = []
        for row in rows:
            record = self.get_run(row[0])
            if record is None:
                raise RuntimeError(  # error-boundary: sealed store read
                    f'reference theater run {row[0]} unreadable')
            records.append(record)
        return records


__all__ = ['CadReferenceTheaterRepository']
