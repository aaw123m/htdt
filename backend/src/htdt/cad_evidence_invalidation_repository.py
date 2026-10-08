"""Append-only persistence for change-diff evidence invalidation
(#964, REV72).

Three tables:

* ``cad_change_diff_records`` — sealed revision-to-revision diffs with
  their emitted semantic change events.
* ``cad_revalidation_queues`` — sealed executable revalidation queues.
* ``cad_revalidation_queue_runs`` — sealed verify-impacts run records
  with per-item outcomes and head-pin drift verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_evidence_invalidation import (
    ChangeDiffRecord,
    RevalidationQueue,
    RevalidationQueueRun,
)


class RevalidationConflictError(ValueError):
    """A revalidation save violated append-only identity."""


class RevalidationIntegrityError(ValueError):
    """A stored revalidation row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise RevalidationIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise RevalidationIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadEvidenceInvalidationRepository:
    """Native storage for the #964 diff/queue/run records."""

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
                'cad_change_diff_records',
                'cad_revalidation_queues',
                'cad_revalidation_queue_runs',
            )

    # ------------------------------------------------------------------
    # Change diff records

    def save_diff_record(self, record: ChangeDiffRecord) -> None:
        _assert_sealed(record, 'diff_sha256', 'diff_id')
        existing = self.get_diff_record(record.diff_id)
        if existing is not None:
            if existing.diff_sha256 == record.diff_sha256:
                return
            raise RevalidationConflictError(
                'change diff records are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_change_diff_records (
                    diff_id, diff_sha256, document_id,
                    from_revision_id, to_revision_id, to_content_hash,
                    recorded_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.diff_id,
                    record.diff_sha256,
                    record.document_id,
                    record.from_revision_id,
                    record.to_revision_id,
                    record.to_content_hash,
                    record.recorded_at_utc,
                    record.model_dump_json(),
                ),
            )

    def get_diff_record(
        self, diff_id: str
    ) -> ChangeDiffRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_change_diff_records WHERE diff_id=?',
                (diff_id,),
            ).fetchone()
        if row is None:
            return None
        record = ChangeDiffRecord.model_validate_json(row['payload_json'])
        if (
            record.diff_id != row['diff_id']
            or record.diff_sha256 != row['diff_sha256']
            or record.document_id != row['document_id']
            or record.from_revision_id != row['from_revision_id']
            or record.to_revision_id != row['to_revision_id']
            or record.to_content_hash != row['to_content_hash']
            or record.recorded_at_utc != row['recorded_at_utc']
        ):
            raise RevalidationIntegrityError(
                'change diff record row disagrees with payload'
            )
        return record

    def list_diff_records(
        self, document_id: str
    ) -> tuple[ChangeDiffRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_change_diff_records '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            ChangeDiffRecord.model_validate_json(r['payload_json'])
            for r in rows
        )

    def diff_record_for_head(
        self, document_id: str, to_revision_id: str
    ) -> ChangeDiffRecord | None:
        """The newest diff record whose target is ``to_revision_id``."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_change_diff_records '
                'WHERE document_id=? AND to_revision_id=? '
                'ORDER BY seq ASC',
                (document_id, to_revision_id),
            ).fetchall()
        if not rows:
            return None
        return ChangeDiffRecord.model_validate_json(
            rows[-1]['payload_json']
        )

    # ------------------------------------------------------------------
    # Revalidation queues

    def save_queue(self, queue: RevalidationQueue) -> None:
        _assert_sealed(queue, 'queue_sha256', 'queue_id')
        existing = self.get_queue(queue.queue_id)
        if existing is not None:
            if existing.queue_sha256 == queue.queue_sha256:
                return
            raise RevalidationConflictError(
                'revalidation queues are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_revalidation_queues (
                    queue_id, queue_sha256, document_id, diff_ref_id,
                    to_revision_id, item_count, software_count,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    queue.queue_id,
                    queue.queue_sha256,
                    queue.document_id,
                    queue.diff_ref.ref_id,
                    queue.to_revision_id,
                    len(queue.items),
                    len(queue.software_sequence),
                    queue.created_at_utc,
                    queue.model_dump_json(),
                ),
            )

    def get_queue(self, queue_id: str) -> RevalidationQueue | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_revalidation_queues WHERE queue_id=?',
                (queue_id,),
            ).fetchone()
        if row is None:
            return None
        queue = RevalidationQueue.model_validate_json(row['payload_json'])
        if (
            queue.queue_id != row['queue_id']
            or queue.queue_sha256 != row['queue_sha256']
            or queue.document_id != row['document_id']
            or queue.diff_ref.ref_id != row['diff_ref_id']
            or queue.to_revision_id != row['to_revision_id']
            or len(queue.items) != row['item_count']
            or len(queue.software_sequence) != row['software_count']
            or queue.created_at_utc != row['created_at_utc']
        ):
            raise RevalidationIntegrityError(
                'revalidation queue row disagrees with payload'
            )
        return queue

    def list_queues(
        self, document_id: str
    ) -> tuple[RevalidationQueue, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_revalidation_queues '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            RevalidationQueue.model_validate_json(r['payload_json'])
            for r in rows
        )

    def latest_queue(self, document_id: str) -> RevalidationQueue | None:
        queues = self.list_queues(document_id)
        return queues[-1] if queues else None

    def queue_for_head(
        self, document_id: str, to_revision_id: str
    ) -> RevalidationQueue | None:
        """The newest queue pinned to ``to_revision_id``."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_revalidation_queues '
                'WHERE document_id=? AND to_revision_id=? '
                'ORDER BY seq ASC',
                (document_id, to_revision_id),
            ).fetchall()
        if not rows:
            return None
        return RevalidationQueue.model_validate_json(
            rows[-1]['payload_json']
        )

    # ------------------------------------------------------------------
    # Queue runs

    def save_run(self, run: RevalidationQueueRun) -> None:
        _assert_sealed(run, 'run_sha256', 'run_id')
        existing = self.get_run(run.run_id)
        if existing is not None:
            if existing.run_sha256 == run.run_sha256:
                return
            raise RevalidationConflictError(
                'revalidation queue runs are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_revalidation_queue_runs (
                    run_id, run_sha256, document_id, queue_ref_id,
                    verdict, started_at_utc, finished_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.run_sha256,
                    run.document_id,
                    run.queue_ref.ref_id,
                    run.verdict,
                    run.started_at_utc,
                    run.finished_at_utc,
                    run.model_dump_json(),
                ),
            )

    def get_run(self, run_id: str) -> RevalidationQueueRun | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_revalidation_queue_runs WHERE run_id=?',
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        run = RevalidationQueueRun.model_validate_json(
            row['payload_json']
        )
        if (
            run.run_id != row['run_id']
            or run.run_sha256 != row['run_sha256']
            or run.document_id != row['document_id']
            or run.queue_ref.ref_id != row['queue_ref_id']
            or run.verdict != row['verdict']
            or run.started_at_utc != row['started_at_utc']
            or run.finished_at_utc != row['finished_at_utc']
        ):
            raise RevalidationIntegrityError(
                'revalidation queue run row disagrees with payload'
            )
        return run

    def list_runs(
        self, document_id: str
    ) -> tuple[RevalidationQueueRun, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_revalidation_queue_runs '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            RevalidationQueueRun.model_validate_json(r['payload_json'])
            for r in rows
        )

    def list_runs_for_queue(
        self, queue_id: str
    ) -> tuple[RevalidationQueueRun, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_revalidation_queue_runs '
                'WHERE queue_ref_id=? ORDER BY seq ASC',
                (queue_id,),
            ).fetchall()
        return tuple(
            RevalidationQueueRun.model_validate_json(r['payload_json'])
            for r in rows
        )

    def latest_run_for_queue(
        self, queue_id: str
    ) -> RevalidationQueueRun | None:
        runs = self.list_runs_for_queue(queue_id)
        return runs[-1] if runs else None


__all__ = [
    'CadEvidenceInvalidationRepository',
    'RevalidationConflictError',
    'RevalidationIntegrityError',
]
