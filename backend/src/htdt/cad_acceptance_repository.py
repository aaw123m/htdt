"""Append-only repository for guided acceptance runs (REV48-HWGUIDE).

Runs live in the canonical authority database (``cad-scenes.sqlite3``) as
revisioned rows: every status change is a new ``(run_id, revision)`` insert
whose ``run_sha256`` chains to the previous revision, so a verifier can
replay what was checked when. Evidence files land in the managed-assets
store (``measurement-assets/``) addressed by digest; the
``htdt_acceptance_evidence`` manifest binds run+step → digest so backups
carry and validate them (``native_backup._ASSET_MANIFEST_TABLES``).
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

from .cad_acceptance import (
    AcceptanceEvidenceRef,
    AcceptanceRun,
    AcceptanceStepRecord,
    next_run_revision,
)
from .cad_schema import (
    NativeSchemaError,
    connect_sqlite,
    ensure_native_schema,
    require_native_tables,
)
from .clock import utc_now_iso
from .managed_assets import MANAGED_ASSETS_DIRNAME, ManagedAssetStore

_RUN_TABLE = 'htdt_acceptance_runs'
_EVIDENCE_TABLE = 'htdt_acceptance_evidence'


class AcceptanceRunRepository:
    """Append-only run store backed by the canonical authority database."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.assets_dir = self.path.parent / MANAGED_ASSETS_DIRNAME
        ensure_native_schema(self.path)
        with closing(connect_sqlite(self.path)) as connection:
            require_native_tables(connection, _RUN_TABLE, _EVIDENCE_TABLE)
            connection.execute(
                'CREATE INDEX IF NOT EXISTS idx_htdt_acceptance_runs_gate '
                'ON htdt_acceptance_runs(gate_id, seq ASC)'
            )
            connection.execute(
                'CREATE INDEX IF NOT EXISTS idx_htdt_acceptance_evidence_run '
                'ON htdt_acceptance_evidence(run_id, step_id)'
            )
            connection.commit()

    @property
    def asset_store(self) -> ManagedAssetStore:
        return ManagedAssetStore(self.assets_dir)

    # ------------------------------------------------------------------
    # writes

    def save(self, run: AcceptanceRun) -> AcceptanceRun:
        """Persist a run revision; recomputes the chained identity hash."""
        with closing(connect_sqlite(self.path)) as connection:
            stored = self._latest_row(connection, run.run_id)
            expected_revision = 1 if stored is None else int(stored[0]) + 1
            if run.revision != expected_revision:
                raise ValueError(
                    f'acceptance run {run.run_id} revision {run.revision} '
                    f'does not follow stored revision '
                    f'{expected_revision - 1}'
                )
            expected_prev = '' if stored is None else str(stored[1])
            if run.prev_run_sha256 != expected_prev:
                raise ValueError(
                    f'acceptance run {run.run_id} revision {run.revision} '
                    'prev_run_sha256 does not match the stored chain head'
                )
            run = run.model_copy(
                update={'run_sha256': run.semantic_sha256()}
            )
            with connection:
                connection.execute(
                    f'INSERT INTO {_RUN_TABLE}('
                    'run_id, revision, gate_id, status, run_sha256, '
                    'payload_json, recorded_at_utc'
                    ') VALUES (?,?,?,?,?,?,?)',
                    (
                        run.run_id,
                        run.revision,
                        run.gate_id,
                        run.status,
                        run.run_sha256,
                        run.model_dump_json(),
                        run.recorded_at_utc,
                    ),
                )
                for step in run.steps:
                    for ref in step.evidence:
                        self._insert_evidence(
                            connection, run.run_id, step.step_id, ref
                        )
        return run

    def commit(
        self,
        run: AcceptanceRun,
        steps: Iterable[AcceptanceStepRecord],
    ) -> AcceptanceRun:
        """Build the next revision from ``steps`` and persist it."""
        return self.save(next_run_revision(run, steps))

    def _insert_evidence(
        self,
        connection,
        run_id: str,
        step_id: str,
        ref: AcceptanceEvidenceRef,
    ) -> None:
        connection.execute(
            f'INSERT OR IGNORE INTO {_EVIDENCE_TABLE}('
            'evidence_id, run_id, step_id, kind, filename, sha256, '
            'relative_path, size_bytes, recorded_at_utc'
            ') VALUES (?,?,?,?,?,?,?,?,?)',
            (
                ref.evidence_id,
                run_id,
                step_id,
                ref.kind,
                ref.filename,
                ref.sha256,
                ref.relative_path,
                ref.size_bytes,
                ref.recorded_at_utc,
            ),
        )

    # ------------------------------------------------------------------
    # reads

    def _latest_row(self, connection, run_id: str):
        return connection.execute(
            f'SELECT revision, run_sha256, payload_json FROM {_RUN_TABLE} '
            'WHERE run_id=? ORDER BY revision DESC LIMIT 1',
            (run_id,),
        ).fetchone()

    def latest(self, run_id: str) -> AcceptanceRun | None:
        with closing(connect_sqlite(self.path)) as connection:
            row = self._latest_row(connection, run_id)
        if row is None:
            return None
        return self.verify_persisted_acceptance_run(
            AcceptanceRun.model_validate_json(row[2])
        )

    def get_revision(
        self, run_id: str, revision: int
    ) -> AcceptanceRun | None:
        """Canonical re-read of one stored revision — the audit replay probe."""
        with closing(connect_sqlite(self.path)) as connection:
            row = connection.execute(
                f'SELECT payload_json FROM {_RUN_TABLE} '
                'WHERE run_id=? AND revision=?',
                (run_id, int(revision)),
            ).fetchone()
        if row is None:
            return None
        return self.verify_persisted_acceptance_run(
            AcceptanceRun.model_validate_json(row[0])
        )

    def revisions(self, run_id: str) -> list[AcceptanceRun]:
        with closing(connect_sqlite(self.path)) as connection:
            rows = connection.execute(
                f'SELECT revision, status, gate_id, run_sha256, payload_json '
                f'FROM {_RUN_TABLE} '
                'WHERE run_id=? ORDER BY revision ASC',
                (run_id,),
            ).fetchall()
        runs: list[AcceptanceRun] = []
        for revision, status, gate_id, run_sha256, payload_json in rows:
            run = AcceptanceRun.model_validate_json(payload_json)
            # The index columns mirror the payload; a divergence is a
            # tamper signal, not a query detail.
            if (
                run.revision != revision
                or run.status != status
                or run.gate_id != gate_id
                or run.run_sha256 != run_sha256
            ):
                raise NativeSchemaError(
                    f'acceptance run {run_id} revision {revision} index '
                    'columns diverge from the stored payload'
                )
            runs.append(run)
        return runs

    def list_runs(
        self, gate_id: str | None = None
    ) -> list[AcceptanceRun]:
        """Latest revision per run, newest first."""
        sql = (
            f'SELECT r1.payload_json FROM {_RUN_TABLE} r1 WHERE '
            'r1.revision = (SELECT MAX(revision) FROM htdt_acceptance_runs '
            'WHERE run_id = r1.run_id)'
        )
        params: tuple[Any, ...] = ()
        if gate_id is not None:
            sql += ' AND r1.gate_id=?'
            params = (gate_id,)
        sql += ' ORDER BY r1.seq DESC'
        with closing(connect_sqlite(self.path)) as connection:
            rows = connection.execute(sql, params).fetchall()
        return [AcceptanceRun.model_validate_json(row[0]) for row in rows]

    def in_progress_runs(self) -> list[AcceptanceRun]:
        return [
            run for run in self.list_runs() if run.status == 'in_progress'
        ]

    def latest_passed_run(self, gate_id: str) -> AcceptanceRun | None:
        for run in self.list_runs(gate_id):
            if run.status == 'passed':
                return run
        return None

    def evidence_for(
        self, run_id: str, step_id: str | None = None
    ) -> list[AcceptanceEvidenceRef]:
        sql = (
            f'SELECT evidence_id, kind, filename, sha256, relative_path, '
            f'size_bytes, recorded_at_utc FROM {_EVIDENCE_TABLE} '
            'WHERE run_id=?'
        )
        params: tuple[Any, ...] = (run_id,)
        if step_id is not None:
            sql += ' AND step_id=?'
            params = (run_id, step_id)
        sql += ' ORDER BY seq ASC'
        with closing(connect_sqlite(self.path)) as connection:
            rows = connection.execute(sql, params).fetchall()
        return [
            AcceptanceEvidenceRef(
                evidence_id=row[0],
                kind=row[1],
                filename=row[2],
                sha256=row[3],
                relative_path=row[4],
                size_bytes=row[5],
                recorded_at_utc=row[6],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # evidence assets

    def attach_evidence(
        self,
        run_id: str,
        step_id: str,
        *,
        kind: str,
        filename: str,
        payload: bytes,
    ) -> AcceptanceEvidenceRef:
        """Install ``payload`` into the managed-assets store and record the
        digest-bound reference immediately (INSERT OR IGNORE on revision
        re-save is idempotent)."""
        import hashlib

        store = self.asset_store
        digest = hashlib.sha256(payload).hexdigest()
        store.ensure_installed(digest, payload)
        ref = AcceptanceEvidenceRef(
            evidence_id=_new_evidence_id(),
            kind=kind,
            filename=filename,
            sha256=digest,
            relative_path=f'{MANAGED_ASSETS_DIRNAME}/{digest}',
            size_bytes=len(payload),
            recorded_at_utc=utc_now_iso(),
        )
        with closing(connect_sqlite(self.path)) as connection, connection:
            self._insert_evidence(connection, run_id, step_id, ref)
        return ref

    # ------------------------------------------------------------------
    # replay probes

    def verify_persisted_acceptance_run(
        self, run: AcceptanceRun
    ) -> AcceptanceRun:
        """Replay-probe: re-derive the identity hash of a stored revision."""
        if run.run_sha256 != run.semantic_sha256():
            raise NativeSchemaError(
                f'acceptance run {run.run_id} revision {run.revision} '
                'fails the identity replay'
            )
        return run

    def verify_run_chain(self, run_id: str) -> AcceptanceRun:
        """Re-derive every revision's hash chain for ``run_id``."""
        revisions = self.revisions(run_id)
        if not revisions:
            raise KeyError(f'unknown acceptance run: {run_id}')
        prev_sha = ''
        for index, revision in enumerate(revisions, start=1):
            if revision.revision != index:
                raise NativeSchemaError(
                    f'acceptance run {run_id} has a gap at revision {index}'
                )
            if revision.prev_run_sha256 != prev_sha:
                raise NativeSchemaError(
                    f'acceptance run {run_id} revision {index} breaks the '
                    'identity chain'
                )
            self.verify_persisted_acceptance_run(revision)
            prev_sha = revision.run_sha256
        return revisions[-1]

    def verify_evidence_integrity(
        self, run_id: str
    ) -> list[tuple[str, str]]:
        """Re-hash every evidence asset; returns (evidence_id, problem)."""
        problems: list[tuple[str, str]] = []
        store = self.asset_store
        for ref in self.evidence_for(run_id):
            if not ref.relative_path.endswith(f'/{ref.sha256}'):
                problems.append((ref.evidence_id, 'path-mismatch'))
                continue
            try:
                content = store.read_verified(ref.sha256)
            except Exception as exc:  # pragma: no cover - corruption path
                problems.append((ref.evidence_id, f'{type(exc).__name__}'))
                continue
            if content is None:
                problems.append((ref.evidence_id, 'missing-asset'))
            elif len(content) != ref.size_bytes:
                problems.append((ref.evidence_id, 'size-mismatch'))
        return problems


def _new_evidence_id() -> str:
    import uuid

    return f'ev-{uuid.uuid4().hex[:16]}'
