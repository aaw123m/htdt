"""Append-only persistence for the #888 headless-CLI authority.

One table — ``cad_headless_run_records`` — behind the shared
``_SealedStore`` machinery (save-time seal re-verification, read-time
column-vs-payload checks). Every mutating ``htdt`` verb seals exactly one
record per invocation, including refused/blocked/cancelled attempts:
an unattended run that left no record is itself an integrity failure.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .cad_calibration_deployment_repository import _SealedStore
from .cad_headless_cli import CadHeadlessRunRecord


class CadHeadlessRunRepository:
    """Native storage for the #888 headless-run authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_headless_run_records')
        self.runs = _SealedStore(
            self._connect,
            'cad_headless_run_records',
            CadHeadlessRunRecord,
            'run_record_id',
            'run_sha256',
            (
                ('document_id', '__document_id__'),
                ('verb', 'verb'),
                ('outcome', 'outcome'),
                ('dry_run', 'dry_run'),
                ('spec_sha256', 'spec_sha256'),
                ('tool_commit_sha', 'tool_commit_sha'),
                ('backend_id', 'backend_id'),
                ('started_at_utc', 'started_at_utc'),
                ('finished_at_utc', 'finished_at_utc'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # -- writes ---------------------------------------------------------------

    def save_run(self, record: CadHeadlessRunRecord) -> None:
        self.runs.save(record)

    # -- reads ----------------------------------------------------------------

    def get_run(self, run_record_id: str) -> CadHeadlessRunRecord | None:
        return self.runs.get(run_record_id)

    def list_runs(
        self,
        document_id: str | None = None,
        *,
        verb: str | None = None,
    ) -> list[CadHeadlessRunRecord]:
        clause = ''
        params: tuple[str, ...] = ()
        if document_id is not None:
            clause = ' WHERE document_id = ?'
            params = (document_id,)
        if verb is not None:
            clause = (
                ' WHERE verb = ?' if not clause
                else clause + ' AND verb = ?')
            params = params + (verb,)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT run_record_id FROM cad_headless_run_records'
                + clause + ' ORDER BY seq ASC',
                params,
            ).fetchall()
        records: list[CadHeadlessRunRecord] = []
        for row in rows:
            record = self.get_run(row[0])
            if record is None:
                raise RuntimeError(  # error-boundary: sealed store read
                    f'headless run record {row[0]} unreadable')
            records.append(record)
        return records


__all__ = ['CadHeadlessRunRepository']
