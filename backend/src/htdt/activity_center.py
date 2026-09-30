"""Application Activity Center (#603).

Today every long-running operation (prediction, search, REW import, project
import/export, backup) is owned by the workspace widget that launched it:
navigation is refused while a worker runs, and an operation detached to
module scope has no application-level presence at all.

This module provides the application-scoped **operation registry** —
independent of any transient workspace widget:

* :class:`ApplicationOperation` — the published read-side snapshot of one
  operation: kind, project/document ref, input authority refs, title,
  state, progress model, cancellability, retryability, result/error
  summary, originating deep link, and currentness;
* :class:`ActivityCenter` — the registry owning lifecycle transitions,
  cooperative cancellation (delegated to the underlying
  :class:`~htdt.native_worker.NativeWorkerPool` contract via cancel
  callbacks), bounded app-local history, navigation blocking for exclusive
  operations, and shutdown accounting.

Deliberate boundaries:

* execution state is never domain result authority — ``COMPLETED`` does not
  promote anything, and a result produced for a superseded input is marked
  ``COMPLETED_FOR_HISTORICAL_INPUT`` rather than discarded;
* progress is reported as the domain supplies it — determinate fraction,
  stage, bytes or items — never a fabricated percentage;
* Cancel is only exposable when the operation declared itself cancellable
  and has not passed its commit point;
* operation history is app-local diagnostics, never a competing scientific
  or evidence authority.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .workflow_navigation import WorkspaceDeepLink


_LOGGER = logging.getLogger(__name__)


ACTIVITY_SCHEMA_VERSION = 1
ACTIVITY_HISTORY_FILENAME = 'activity_history.json'
ACTIVITY_HISTORY_LIMIT = 200


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace(
        '+00:00', 'Z'
    )


class OperationClass(StrEnum):
    COMPUTE = 'compute'
    EXTERNAL_IO = 'external_io'
    DATA_MANAGEMENT = 'data_management'


class OperationState(StrEnum):
    QUEUED = 'queued'
    PREFLIGHTING = 'preflighting'
    RUNNING = 'running'
    CANCELLATION_REQUESTED = 'cancellation_requested'
    CANCELLED = 'cancelled'
    COMPLETED = 'completed'
    FAILED = 'failed'
    COMPLETED_FOR_HISTORICAL_INPUT = 'completed_for_historical_input'
    RESULT_STALE = 'result_stale'


TERMINAL_STATES = frozenset(
    {
        OperationState.CANCELLED,
        OperationState.COMPLETED,
        OperationState.FAILED,
        OperationState.COMPLETED_FOR_HISTORICAL_INPUT,
        OperationState.RESULT_STALE,
    }
)


class ProgressKind(StrEnum):
    INDETERMINATE = 'indeterminate'
    DETERMINATE = 'determinate'
    STAGE = 'stage'
    BYTES = 'bytes'
    ITEMS = 'items'


class OperationProgress(BaseModel):
    """Progress as the domain reports it — never a fabricated percentage."""

    model_config = ConfigDict(frozen=True)

    kind: ProgressKind = ProgressKind.INDETERMINATE
    fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    stage_index: int | None = Field(default=None, ge=0)
    stage_count: int | None = Field(default=None, ge=1)
    stage_label: str | None = None
    done_units: int | None = Field(default=None, ge=0)
    total_units: int | None = Field(default=None, ge=0)
    unit_label: str | None = None

    @model_validator(mode='after')
    def valid_progress(self) -> 'OperationProgress':
        if self.kind == ProgressKind.DETERMINATE and self.fraction is None:
            raise ValueError('determinate progress requires fraction')
        if self.kind == ProgressKind.STAGE and (
            self.stage_index is None or self.stage_count is None
        ):
            raise ValueError('stage progress requires stage_index and stage_count')
        if self.kind == ProgressKind.STAGE and self.stage_index is not None and (
            self.stage_count is None or self.stage_index > self.stage_count
        ):
            raise ValueError('stage_index exceeds stage_count')
        if self.kind in (ProgressKind.BYTES, ProgressKind.ITEMS) and (
            self.done_units is None or self.total_units is None
        ):
            raise ValueError(f'{self.kind} progress requires done/total units')
        if self.kind == ProgressKind.INDETERMINATE and (
            self.fraction is not None
            or self.done_units is not None
            or self.stage_index is not None
        ):
            raise ValueError('indeterminate progress cannot carry concrete values')
        return self

    def known_fraction(self) -> float | None:
        """Best-known completion fraction; None when honestly unknown."""

        if self.kind == ProgressKind.DETERMINATE:
            return self.fraction
        if self.kind == ProgressKind.STAGE and self.stage_count:
            return self.stage_index / self.stage_count if self.stage_index else 0.0
        if self.kind in (ProgressKind.BYTES, ProgressKind.ITEMS) and self.total_units:
            return min(1.0, (self.done_units or 0) / self.total_units)
        return None


class Cancellability(StrEnum):
    CANCELLABLE = 'cancellable'
    NOT_CANCELLABLE = 'not_cancellable'
    CANCEL_UNTIL_COMMIT = 'cancel_until_commit'


class RetryPolicy(StrEnum):
    NONE = 'none'
    SAFE_NEW_ATTEMPT = 'safe_new_attempt'
    UNSAFE = 'unsafe'


class NavigationPolicy(StrEnum):
    """Whether other work may proceed while the operation runs."""

    BACKGROUNDABLE = 'backgroundable'
    EXCLUSIVE = 'exclusive'


class ApplicationOperation(BaseModel):
    """Published read-side snapshot of one application operation.

    Immutable: the registry publishes a new snapshot per transition.
    """

    model_config = ConfigDict(frozen=True)

    operation_id: str = Field(min_length=1)
    operation_kind: str = Field(min_length=1)
    operation_class: OperationClass
    title: str = Field(min_length=1)
    project_ref: str | None = None
    document_ref: str | None = None
    input_authority_refs: tuple[str, ...] = ()
    revision_ref: str | None = None
    state: OperationState = OperationState.QUEUED
    progress: OperationProgress = OperationProgress()
    cancellability: Cancellability = Cancellability.NOT_CANCELLABLE
    cancel_committed: bool = False
    retry_policy: RetryPolicy = RetryPolicy.NONE
    retry_of: str | None = None
    attempt: int = 1
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str = Field(min_length=1)
    result_summary: str | None = None
    error_summary: str | None = None
    diagnostic_id: str | None = None
    deep_link: WorkspaceDeepLink | None = None
    navigation_policy: NavigationPolicy = NavigationPolicy.BACKGROUNDABLE
    navigation_block_reason: str | None = None
    current_for_input: bool = True

    @model_validator(mode='after')
    def valid_operation(self) -> 'ApplicationOperation':
        if self.state in TERMINAL_STATES and self.finished_at is None:
            raise ValueError('terminal operation requires finished_at')
        if self.state in (
            OperationState.RUNNING,
            OperationState.CANCELLATION_REQUESTED,
        ) and self.started_at is None:
            raise ValueError('running operation requires started_at')
        if self.navigation_policy == NavigationPolicy.EXCLUSIVE and not (
            self.navigation_block_reason
        ):
            raise ValueError('exclusive operations require a block reason')
        if self.retry_of is not None and self.attempt < 2:
            raise ValueError('retry operations must have attempt >= 2')
        if self.error_summary and self.state != OperationState.FAILED:
            raise ValueError('error_summary requires failed state')
        return self

    @property
    def is_active(self) -> bool:
        return self.state in (
            OperationState.QUEUED,
            OperationState.PREFLIGHTING,
            OperationState.RUNNING,
            OperationState.CANCELLATION_REQUESTED,
        )

    @property
    def can_cancel_now(self) -> bool:
        if not self.is_active or self.cancel_committed:
            return False
        return self.cancellability in (
            Cancellability.CANCELLABLE,
            Cancellability.CANCEL_UNTIL_COMMIT,
        )


class OperationTransitionError(ValueError):
    pass


_ALLOWED_TRANSITIONS: dict[OperationState, frozenset[OperationState]] = {
    OperationState.QUEUED: frozenset(
        {
            OperationState.PREFLIGHTING,
            OperationState.RUNNING,
            OperationState.CANCELLATION_REQUESTED,
            OperationState.CANCELLED,
            OperationState.FAILED,
        }
    ),
    OperationState.PREFLIGHTING: frozenset(
        {
            OperationState.RUNNING,
            OperationState.CANCELLATION_REQUESTED,
            OperationState.CANCELLED,
            OperationState.FAILED,
        }
    ),
    OperationState.RUNNING: frozenset(
        {
            OperationState.CANCELLATION_REQUESTED,
            OperationState.CANCELLED,
            OperationState.COMPLETED,
            OperationState.FAILED,
        }
    ),
    OperationState.CANCELLATION_REQUESTED: frozenset(
        {
            OperationState.CANCELLED,
            OperationState.COMPLETED,
            OperationState.FAILED,
        }
    ),
    # Terminal states never transition.
    OperationState.CANCELLED: frozenset(),
    OperationState.COMPLETED: frozenset(
        {
            OperationState.COMPLETED_FOR_HISTORICAL_INPUT,
            OperationState.RESULT_STALE,
        }
    ),
    OperationState.FAILED: frozenset(),
    OperationState.COMPLETED_FOR_HISTORICAL_INPUT: frozenset(),
    OperationState.RESULT_STALE: frozenset(),
}


@dataclass(slots=True)
class _OperationRecord:
    """Mutable bookkeeping for one registered operation."""

    snapshot: ApplicationOperation
    cancel_callback: Callable[[], None] | None = None
    domain_payload: Any = None


@dataclass(frozen=True, slots=True)
class OperationRetryRequest:
    """Explicit executor wiring for one safe new attempt (#738).

    The previous attempt's cancel callback is deliberately cleared at its
    terminal transition, so a retry must never resurrect it. The owning
    operation adapter supplies a fresh callback bound to the new executor
    submission — without one the new attempt is honestly non-cancellable.
    """

    domain_payload: Any = None
    cancel_callback: Callable[[], None] | None = None


@dataclass(frozen=True, slots=True)
class ShutdownReport:
    """App-exit accounting for active operations."""

    active_at_exit: tuple[ApplicationOperation, ...]
    cancellation_requested: tuple[str, ...]
    detached_lingering: tuple[ApplicationOperation, ...]
    blocking_exclusive: tuple[ApplicationOperation, ...]


class ActivityCenter:
    """Application-scoped operation registry.

    One instance per application; workspace controllers publish operation
    state instead of owning worker lifetimes. Cancellation reuses the
    cooperative contract of the underlying executor: ``request_cancel``
    flips state and invokes the registered cancel callback (typically
    ``NativeWorker.cancel``); it never pretends a QThread keeps running
    after process exit.
    """

    def __init__(
        self,
        *,
        history_limit: int = ACTIVITY_HISTORY_LIMIT,
        record_limit: int | None = None,
    ) -> None:
        self._records: dict[str, _OperationRecord] = {}
        self._history: list[ApplicationOperation] = []
        self._history_limit = history_limit
        # Full records (cancel path + domain payload) are only retained for
        # active operations plus a bounded tail of recent terminal ones —
        # the history row stays the canonical long-term entry, so a busy
        # session cannot pile one record per operation ever submitted.
        self._record_limit = (
            history_limit if record_limit is None else record_limit
        )
        self._listeners: list[Callable[[ApplicationOperation], None]] = []

    # -- registration ----------------------------------------------------

    def subscribe(self, listener: Callable[[ApplicationOperation], None]) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[ApplicationOperation], None]) -> None:
        """Detach a previously subscribed listener (idempotent).

        The registry outlives workspace mounts: a listener owned by a
        disposed mount must be removed, or every later transition keeps
        invoking a dead observer and retaining its closure forever.
        """

        self._listeners = [
            existing for existing in self._listeners if existing != listener
        ]

    def _emit(self, record: _OperationRecord) -> None:
        self._emit_snapshot(record.snapshot)

    def _emit_snapshot(self, snapshot: ApplicationOperation) -> None:
        for listener in tuple(self._listeners):
            listener(snapshot)

    def _archive(self, record: _OperationRecord) -> None:
        # One operation_id (one attempt) occupies one logical history row:
        # a post-completion reclassification (COMPLETED ->
        # COMPLETED_FOR_HISTORICAL_INPUT / RESULT_STALE) replaces its row
        # instead of appending a duplicate (#738).
        for index, existing in enumerate(self._history):
            if existing.operation_id == record.snapshot.operation_id:
                self._history[index] = record.snapshot
                break
        else:
            self._history.append(record.snapshot)
        if len(self._history) > self._history_limit:
            del self._history[: len(self._history) - self._history_limit]
        # Evict the oldest terminal records once the registry grows past
        # the record budget; active records and the bounded recent tail
        # are retained so get()/retry() still resolve for operations the
        # operator can still see.
        excess = len(self._records) - self._record_limit
        if excess > 0:
            for operation_id in list(self._records):
                if excess <= 0:
                    break
                if self._records[operation_id].snapshot.is_active:
                    continue
                del self._records[operation_id]
                excess -= 1

    def submit(
        self,
        *,
        operation_kind: str,
        operation_class: OperationClass,
        title: str,
        project_ref: str | None = None,
        document_ref: str | None = None,
        input_authority_refs: Iterable[str] = (),
        revision_ref: str | None = None,
        cancellability: Cancellability = Cancellability.NOT_CANCELLABLE,
        retry_policy: RetryPolicy = RetryPolicy.NONE,
        navigation_policy: NavigationPolicy = NavigationPolicy.BACKGROUNDABLE,
        navigation_block_reason: str | None = None,
        deep_link: WorkspaceDeepLink | None = None,
        cancel_callback: Callable[[], None] | None = None,
        domain_payload: Any = None,
        operation_id: str | None = None,
        retry_of: str | None = None,
        attempt: int = 1,
    ) -> str:
        operation_id = operation_id or f'op-{uuid.uuid4().hex[:12]}'
        if (
            operation_id in self._records
            or self._history_snapshot(operation_id) is not None
        ):
            raise OperationTransitionError(f'duplicate operation id {operation_id}')
        if (
            retry_of is not None
            and retry_of not in self._records
            and self._history_snapshot(retry_of) is None
        ):
            raise OperationTransitionError(
                f'retry references unknown operation {retry_of}'
            )
        if cancellability == Cancellability.NOT_CANCELLABLE and cancel_callback is not None:
            raise OperationTransitionError(
                'a non-cancellable operation cannot register a cancel callback'
            )
        if (
            cancellability != Cancellability.NOT_CANCELLABLE
            and cancel_callback is None
        ):
            raise OperationTransitionError(
                'a cancellable operation requires a live cancel callback'
            )
        snapshot = ApplicationOperation(
            operation_id=operation_id,
            operation_kind=operation_kind,
            operation_class=operation_class,
            title=title,
            project_ref=project_ref,
            document_ref=document_ref,
            input_authority_refs=tuple(input_authority_refs),
            revision_ref=revision_ref,
            cancellability=cancellability,
            retry_policy=retry_policy,
            retry_of=retry_of,
            attempt=attempt,
            navigation_policy=navigation_policy,
            navigation_block_reason=navigation_block_reason,
            deep_link=deep_link,
            updated_at=_utc_now(),
        )
        record = _OperationRecord(
            snapshot=snapshot,
            cancel_callback=cancel_callback,
            domain_payload=domain_payload,
        )
        self._records[operation_id] = record
        self._emit(record)
        return operation_id

    # -- read ------------------------------------------------------------

    def _history_snapshot(
        self, operation_id: str
    ) -> ApplicationOperation | None:
        for snapshot in reversed(self._history):
            if snapshot.operation_id == operation_id:
                return snapshot
        return None

    def get(self, operation_id: str) -> ApplicationOperation | None:
        record = self._records.get(operation_id)
        if record is not None:
            return record.snapshot
        return self._history_snapshot(operation_id)

    def require(self, operation_id: str) -> ApplicationOperation:
        snapshot = self.get(operation_id)
        if snapshot is None:
            raise KeyError(f'unknown operation {operation_id}')
        return snapshot

    def active(self) -> tuple[ApplicationOperation, ...]:
        return tuple(
            r.snapshot for r in self._records.values() if r.snapshot.is_active
        )

    def recent(self, limit: int = 20) -> tuple[ApplicationOperation, ...]:
        return tuple(self._history[-limit:])

    def failed(self, limit: int = 20) -> tuple[ApplicationOperation, ...]:
        failures = [op for op in self._history if op.state == OperationState.FAILED]
        return tuple(failures[-limit:])

    def navigation_blockers(self) -> tuple[ApplicationOperation, ...]:
        """Exclusive active operations that block navigation/data writes."""

        return tuple(
            op
            for op in self.active()
            if op.navigation_policy == NavigationPolicy.EXCLUSIVE
        )

    def navigation_blocked(self) -> bool:
        return bool(self.navigation_blockers())

    # -- transitions -----------------------------------------------------

    def _record(self, operation_id: str) -> _OperationRecord:
        record = self._records.get(operation_id)
        if record is None:
            raise KeyError(f'unknown operation {operation_id}')
        return record

    def _transition(
        self,
        operation_id: str,
        target: OperationState,
        **updates: Any,
    ) -> ApplicationOperation:
        record = self._record(operation_id)
        current = record.snapshot
        allowed = _ALLOWED_TRANSITIONS[current.state]
        if target not in allowed:
            raise OperationTransitionError(
                f'operation {operation_id}: {current.state} -> {target} not allowed'
            )
        fields: dict[str, Any] = {'updated_at': _utc_now()}
        if target in (
            OperationState.RUNNING,
        ) and current.started_at is None:
            fields['started_at'] = _utc_now()
        if target in TERMINAL_STATES:
            if current.state not in TERMINAL_STATES:
                fields['finished_at'] = _utc_now()
            record.cancel_callback = None
        fields.update(updates)
        record.snapshot = current.model_copy(update={'state': target, **fields})
        self._emit(record)
        if target in TERMINAL_STATES:
            self._archive(record)
        return record.snapshot

    def mark_preflighting(self, operation_id: str) -> ApplicationOperation:
        return self._transition(operation_id, OperationState.PREFLIGHTING)

    def mark_running(
        self, operation_id: str, progress: OperationProgress | None = None
    ) -> ApplicationOperation:
        updates: dict[str, Any] = {}
        if progress is not None:
            updates['progress'] = progress
        return self._transition(operation_id, OperationState.RUNNING, **updates)

    def update_progress(
        self, operation_id: str, progress: OperationProgress
    ) -> ApplicationOperation:
        record = self._record(operation_id)
        if not record.snapshot.is_active:
            raise OperationTransitionError(
                f'cannot update progress of {operation_id} in {record.snapshot.state}'
            )
        record.snapshot = record.snapshot.model_copy(
            update={'progress': progress, 'updated_at': _utc_now()}
        )
        self._emit(record)
        return record.snapshot

    def mark_commit_point(self, operation_id: str) -> ApplicationOperation:
        """Operation passed the point where cancellation is no longer safe."""

        record = self._record(operation_id)
        if not record.snapshot.is_active:
            raise OperationTransitionError('commit point on inactive operation')
        record.snapshot = record.snapshot.model_copy(
            update={'cancel_committed': True, 'updated_at': _utc_now()}
        )
        self._emit(record)
        return record.snapshot

    def complete(
        self, operation_id: str, *, result_summary: str | None = None
    ) -> ApplicationOperation:
        return self._transition(
            operation_id, OperationState.COMPLETED, result_summary=result_summary
        )

    def fail(
        self,
        operation_id: str,
        *,
        error_summary: str,
        diagnostic_id: str | None = None,
    ) -> ApplicationOperation:
        return self._transition(
            operation_id,
            OperationState.FAILED,
            error_summary=error_summary,
            diagnostic_id=diagnostic_id,
        )

    def request_cancel(self, operation_id: str) -> bool:
        """Request cooperative cancellation; False when not exposable.

        Only a declared-cancellable operation that has not passed its commit
        point can be cancelled — the UI must never offer Cancel otherwise.
        """

        record = self._record(operation_id)
        snapshot = record.snapshot
        if not snapshot.can_cancel_now:
            return False
        self._transition(operation_id, OperationState.CANCELLATION_REQUESTED)
        callback = record.cancel_callback
        if callback is not None:
            callback()
        return True

    def confirm_cancelled(self, operation_id: str) -> ApplicationOperation:
        """The executor reports the cancellation took effect."""

        return self._transition(operation_id, OperationState.CANCELLED)

    # -- currency / staleness -------------------------------------------

    def note_authorities_changed(self, authority_refs: Iterable[str]) -> None:
        """Mark operations whose inputs are no longer current.

        A completed operation bound to a superseded input becomes
        ``COMPLETED_FOR_HISTORICAL_INPUT`` — the result stays valid evidence
        for its original input and is never discarded just because the UI
        moved on.
        """

        changed = set(authority_refs)
        for record in self._records.values():
            snapshot = record.snapshot
            if not snapshot.input_authority_refs:
                continue
            if not changed.intersection(snapshot.input_authority_refs):
                if snapshot.current_for_input:
                    record.snapshot = snapshot.model_copy(
                        update={'updated_at': _utc_now()}
                    )
                continue
            if not snapshot.current_for_input:
                continue
            if snapshot.state == OperationState.COMPLETED:
                self._transition(
                    snapshot.operation_id,
                    OperationState.COMPLETED_FOR_HISTORICAL_INPUT,
                    current_for_input=False,
                )
            else:
                record.snapshot = snapshot.model_copy(
                    update={'current_for_input': False, 'updated_at': _utc_now()}
                )
                self._emit(record)
        # Terminal records evicted from ``_records`` survive only as bounded
        # history rows — apply the same reclassification in place so the
        # rendered history still marks stale inputs without retaining the
        # full records forever.
        for index, snapshot in enumerate(self._history):
            if snapshot.operation_id in self._records:
                continue
            if not snapshot.input_authority_refs:
                continue
            if not changed.intersection(snapshot.input_authority_refs):
                if snapshot.current_for_input:
                    self._history[index] = snapshot.model_copy(
                        update={'updated_at': _utc_now()}
                    )
                continue
            if not snapshot.current_for_input:
                continue
            update: dict[str, Any] = {
                'current_for_input': False,
                'updated_at': _utc_now(),
            }
            if snapshot.state == OperationState.COMPLETED:
                update['state'] = OperationState.COMPLETED_FOR_HISTORICAL_INPUT
            self._history[index] = snapshot.model_copy(update=update)
            self._emit_snapshot(self._history[index])

    # -- retry -----------------------------------------------------------

    def retry(
        self,
        operation_id: str,
        *,
        retry_factory: Callable[
            [ApplicationOperation], OperationRetryRequest
        ] | None = None,
    ) -> str:
        """Create a new attempt identity for a failed/cancelled operation.

        ``retry_factory`` is the explicit adapter contract that resubmits
        the operation to its executor and returns the fresh per-attempt
        wiring (domain payload + live cancel callback). The previous
        attempt's callback was cleared at its terminal transition, so the
        new attempt only advertises cancellability when the factory
        supplies a real delivery path.
        """

        record = self._records.get(operation_id)
        snapshot = (
            record.snapshot
            if record is not None
            else self._history_snapshot(operation_id)
        )
        if snapshot is None:
            raise KeyError(f'unknown operation {operation_id}')
        if snapshot.retry_policy != RetryPolicy.SAFE_NEW_ATTEMPT:
            raise OperationTransitionError(
                f'operation {operation_id} is not safely retryable'
            )
        if snapshot.is_active:
            raise OperationTransitionError('cannot retry an active operation')
        request = (
            retry_factory(snapshot)
            if retry_factory is not None
            else OperationRetryRequest()
        )
        return self.submit(
            operation_kind=snapshot.operation_kind,
            operation_class=snapshot.operation_class,
            title=snapshot.title,
            project_ref=snapshot.project_ref,
            document_ref=snapshot.document_ref,
            input_authority_refs=snapshot.input_authority_refs,
            revision_ref=snapshot.revision_ref,
            cancellability=(
                snapshot.cancellability
                if request.cancel_callback is not None
                else Cancellability.NOT_CANCELLABLE
            ),
            retry_policy=snapshot.retry_policy,
            navigation_policy=snapshot.navigation_policy,
            navigation_block_reason=snapshot.navigation_block_reason,
            deep_link=snapshot.deep_link,
            cancel_callback=request.cancel_callback,
            domain_payload=(
                request.domain_payload
                if request.domain_payload is not None
                else (record.domain_payload if record is not None else None)
            ),
            retry_of=snapshot.operation_id,
            attempt=snapshot.attempt + 1,
        )

    # -- shutdown --------------------------------------------------------

    def prepare_shutdown(
        self,
        *,
        request_cancellation: bool = True,
    ) -> ShutdownReport:
        """Account for active operations before process exit.

        Cancellable actives get a cancellation request; anything still
        running past the executor's own shutdown budget is reported as
        detached/lingering — a diagnostic condition, never a promise that an
        in-process worker survives process exit.
        """

        active = self.active()
        cancelled: list[str] = []
        for op in active:
            if request_cancellation and op.can_cancel_now:
                if self.request_cancel(op.operation_id):
                    cancelled.append(op.operation_id)
        remaining = self.active()
        # Every operation still active at teardown is reported as
        # lingering (#738) — not just cancellation-requested ones. A
        # non-cancellable RUNNING operation is the work most likely to be
        # impossible to stop, and its snapshot carries state and
        # cancellability so shutdown presentation can distinguish
        # 'cancel requested' from 'cannot cancel'.
        return ShutdownReport(
            active_at_exit=tuple(active),
            cancellation_requested=tuple(cancelled),
            detached_lingering=tuple(remaining),
            blocking_exclusive=self.navigation_blockers(),
        )

    # -- app-local history persistence -----------------------------------

    def persist_history(self, path: Path) -> None:
        """Write the bounded recent/failed history (diagnostics only)."""

        payload = {
            'schema_version': ACTIVITY_SCHEMA_VERSION,
            'authority': 'htdt-activity-center-history',
            'operations': [op.model_dump(mode='json') for op in self._history],
            # Last-known active-operation context (#738): after a crash the
            # next session can say what was running when the prior session
            # ended. Diagnostics only — never treated as resumable work.
            'active_operations': [
                op.model_dump(mode='json') for op in self.active()
            ],
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + '.', suffix='.tmp'
        )
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def _load_payload(cls, path: Path) -> dict | None:
        """Read the persisted activity payload; corrupt files degrade to
        ``None`` — diagnostics must never crash on a torn sidecar."""

        path = Path(path)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            _LOGGER.warning(
                'activity record unreadable, ignoring %s: %s', path, exc
            )
            return None
        if not isinstance(payload, dict):
            _LOGGER.warning('activity record is not an object: %s', path)
            return None
        if payload.get('schema_version') != ACTIVITY_SCHEMA_VERSION:
            return None
        return payload

    @classmethod
    def load_history(cls, path: Path) -> tuple[ApplicationOperation, ...]:
        payload = cls._load_payload(path)
        if payload is None:
            return ()
        return cls._load_operations(payload.get('operations', ()), Path(path))

    @classmethod
    def load_active_operations(
        cls, path: Path
    ) -> tuple[ApplicationOperation, ...]:
        """Last-known active operations from a possibly-crashed session.

        Recovery diagnostics (#604/#739) read this to identify what was
        running when the previous session ended; the entries are never
        treated as resumable work.
        """

        payload = cls._load_payload(path)
        if payload is None:
            return ()
        return cls._load_operations(
            payload.get('active_operations', ()), Path(path)
        )

    @staticmethod
    def _load_operations(
        items: object, path: Path
    ) -> tuple[ApplicationOperation, ...]:
        """Validate rows individually: one corrupt or hand-edited row must
        not take down every other row of a diagnostics read.
        """
        if not isinstance(items, list):
            return ()
        operations: list[ApplicationOperation] = []
        for item in items:
            try:
                operations.append(
                    ApplicationOperation.model_validate(item)
                )
            except Exception:
                _LOGGER.warning(
                    'dropping invalid activity record from %s', path
                )
        return tuple(operations)


__all__ = [
    'ACTIVITY_HISTORY_FILENAME',
    'ACTIVITY_HISTORY_LIMIT',
    'ACTIVITY_SCHEMA_VERSION',
    'ActivityCenter',
    'ApplicationOperation',
    'Cancellability',
    'NavigationPolicy',
    'OperationClass',
    'OperationProgress',
    'OperationRetryRequest',
    'OperationState',
    'OperationTransitionError',
    'ProgressKind',
    'RetryPolicy',
    'ShutdownReport',
    'TERMINAL_STATES',
]
