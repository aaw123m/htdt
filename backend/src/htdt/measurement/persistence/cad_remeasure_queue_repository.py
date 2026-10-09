"""Append-only persistence for the #968 re-measurement queue authority.

Two tables: ``cad_remeasure_queues`` stores each sealed deterministic
queue (identical evaluation set → identical queue → idempotent save);
``cad_remeasure_queue_events`` stores the sealed terminal transitions
(dismissed / converted) of individual queued items. Reads re-validate
the queue's scene binding so a queue built on a foreign or tampered
scene cannot silently persist.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from ...cad_repository import SceneRepository
from ...cad_schema import connect_sqlite, require_native_tables
from ...clock import utc_now_iso as _utc_now
from ..domain.cad_remeasure_queue import (
    RemeasureItemState,
    RemeasureQueue,
    RemeasureQueueEvent,
    resolve_item_states,
)


class RemeasureQueueError(ValueError):
    pass


class CadRemeasureQueueRepository:
    """Native storage for sealed re-measurement queues and their events."""

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
                'cad_remeasure_queues',
                'cad_remeasure_queue_events',
            )

    # -- scene binding ---------------------------------------------------

    def _validate_queue_scene_binding(self, queue: RemeasureQueue) -> None:
        revision = self.scene_repository.get(queue.scene_revision_id)
        if revision is None:
            raise RemeasureQueueError(
                're-measurement queue scene revision is not persisted'
            )
        if revision.document_id != queue.document_id:
            raise RemeasureQueueError(
                're-measurement queue binds a foreign project scene'
            )
        if revision.content_hash != queue.scene_content_hash:
            raise RemeasureQueueError(
                're-measurement queue scene content hash mismatch'
            )

    # -- queues -----------------------------------------------------------

    def save_queue(self, queue: RemeasureQueue) -> None:
        """Append the sealed queue; identical queues are idempotent."""
        self._validate_queue_scene_binding(queue)
        existing = self.get_queue(queue.queue_id)
        if existing is not None:
            if existing.queue_sha256 == queue.queue_sha256:
                return
            raise RemeasureQueueError(
                're-measurement queues are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_remeasure_queues (
                    queue_id, queue_sha256, document_id, scene_revision_id,
                    evaluation_set_sha256, item_count, created_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    queue.queue_id,
                    queue.queue_sha256,
                    queue.document_id,
                    queue.scene_revision_id,
                    queue.evaluation_set_sha256,
                    len(queue.items),
                    _utc_now(),
                    queue.model_dump_json(),
                ),
            )

    def get_queue(self, queue_id: str) -> RemeasureQueue | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_remeasure_queues '
                'WHERE queue_id=?',
                (queue_id,),
            ).fetchone()
        if row is None:
            return None
        queue = RemeasureQueue.model_validate_json(row['payload_json'])
        self._validate_queue_scene_binding(queue)
        return queue

    def list_queues(self, document_id: str) -> tuple[RemeasureQueue, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT queue_id FROM cad_remeasure_queues '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        queues: list[RemeasureQueue] = []
        for row in rows:
            queue = self.get_queue(str(row['queue_id']))
            if queue is None:
                raise RuntimeError(  # error-boundary: sealed read
                    f're-measurement queue {row["queue_id"]} unreadable'
                )
            queues.append(queue)
        return tuple(queues)

    def latest_queue(self, document_id: str) -> RemeasureQueue | None:
        queues = self.list_queues(document_id)
        return queues[-1] if queues else None

    def queue_created_at_utc(self, document_id: str) -> dict[str, str]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT queue_id, created_at_utc FROM cad_remeasure_queues '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return {
            str(row['queue_id']): str(row['created_at_utc']) for row in rows
        }

    # -- events -------------------------------------------------------------

    def append_event(self, event: RemeasureQueueEvent) -> None:
        """Append a sealed terminal transition for one queued item.

        The event must pin the persisted queue's exact sha, name one of
        its queued items, and be the first terminal transition for that
        item — item history is append-only and never rewritten.
        """
        queue = self.get_queue(event.queue_id)
        if queue is None:
            raise RemeasureQueueError(
                're-measurement queue event binds an unknown queue'
            )
        if queue.queue_sha256 != event.queue_sha256:
            raise RemeasureQueueError(
                're-measurement queue event pins a different queue revision'
            )
        if queue.item(event.measurement_id) is None:
            raise RemeasureQueueError(
                're-measurement queue event names an unqueued measurement'
            )
        existing = self.get_event(event.event_id)
        if existing is not None:
            if existing.event_sha256 == event.event_sha256:
                return
            raise RemeasureQueueError(
                're-measurement queue events are append-only'
            )
        states = resolve_item_states(queue, self.list_events(queue.queue_id))
        if states.get(event.measurement_id) != 'pending':
            raise RemeasureQueueError(
                're-measurement queue item already has a terminal state'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_remeasure_queue_events (
                    event_id, event_sha256, queue_id, measurement_id,
                    kind, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.event_sha256,
                    event.queue_id,
                    event.measurement_id,
                    event.kind,
                    event.created_at_utc,
                    event.model_dump_json(),
                ),
            )

    def get_event(self, event_id: str) -> RemeasureQueueEvent | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_remeasure_queue_events '
                'WHERE event_id=?',
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        return RemeasureQueueEvent.model_validate_json(row['payload_json'])

    def list_events(self, queue_id: str) -> tuple[RemeasureQueueEvent, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_remeasure_queue_events '
                'WHERE queue_id=? ORDER BY seq ASC',
                (queue_id,),
            ).fetchall()
        return tuple(
            RemeasureQueueEvent.model_validate_json(row['payload_json'])
            for row in rows
        )

    def item_states(
        self, queue: RemeasureQueue
    ) -> dict[str, RemeasureItemState]:
        """Resolved per-item states for one persisted queue."""
        for event in self.list_events(queue.queue_id):
            if event.queue_sha256 != queue.queue_sha256:
                raise RemeasureQueueError(
                    're-measurement queue event binds another queue revision'
                )
        return resolve_item_states(
            queue, self.list_events(queue.queue_id)
        )


__all__ = ['CadRemeasureQueueRepository', 'RemeasureQueueError']
