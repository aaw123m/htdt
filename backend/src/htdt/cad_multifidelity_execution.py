from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_multifidelity import (
    MultiFidelityAuthorityRef,
    MultiFidelityPlan,
)
from .cad_repository import SceneRepository
from .cad_schema import ensure_native_schema


R140_EXECUTION_SCHEMA_VERSION = 1
R140_EXECUTION_TASK_AUTHORITY_VERSION = 'r140-multifidelity-task-1'
R140_EXECUTION_SCHEDULE_AUTHORITY_VERSION = 'r140-multifidelity-schedule-1'
R140_EXECUTION_CACHE_AUTHORITY_VERSION = 'r140-multifidelity-cache-1'
R140_SCHEDULER_ALGORITHM_VERSION = 'r140-bounded-batching-1'


ExternalAuthorityResolver = Callable[
    [MultiFidelityAuthorityRef],
    MultiFidelityAuthorityRef | None,
]


class MultiFidelityPlanResolver(Protocol):
    path: Path

    def get_plan(self, plan_id: str) -> MultiFidelityPlan | None:
        ...


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecutionResourceVector(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    cpu_threads: int = Field(ge=0)
    gpu_slots: int = Field(ge=0)
    memory_bytes: int = Field(ge=0)
    scratch_bytes: int = Field(ge=0)

    @model_validator(mode='after')
    def non_empty(self) -> 'ExecutionResourceVector':
        if (
            self.cpu_threads == 0
            and self.gpu_slots == 0
            and self.memory_bytes == 0
            and self.scratch_bytes == 0
        ):
            raise ValueError('execution resource vector cannot be entirely zero')
        return self

    def plus(self, other: 'ExecutionResourceVector') -> 'ExecutionResourceVector':
        return ExecutionResourceVector(
            cpu_threads=self.cpu_threads + other.cpu_threads,
            gpu_slots=self.gpu_slots + other.gpu_slots,
            memory_bytes=self.memory_bytes + other.memory_bytes,
            scratch_bytes=self.scratch_bytes + other.scratch_bytes,
        )

    def fits_within(self, capacity: 'ExecutionResourceVector') -> bool:
        return (
            self.cpu_threads <= capacity.cpu_threads
            and self.gpu_slots <= capacity.gpu_slots
            and self.memory_bytes <= capacity.memory_bytes
            and self.scratch_bytes <= capacity.scratch_bytes
        )


class ExecutionCapacityAuthority(BaseModel):
    """Exact hardware/resource capacity used only for deterministic admission."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    capacity_authority_ref: MultiFidelityAuthorityRef
    capacity: ExecutionResourceVector


class MultiFidelityExecutionTask(BaseModel):
    """Exact stage/candidate execution identity.

    This is a dispatch/cache key authority. It does not execute work.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = R140_EXECUTION_SCHEMA_VERSION
    authority_version: Literal[
        'r140-multifidelity-task-1'
    ] = R140_EXECUTION_TASK_AUTHORITY_VERSION

    task_id: str = Field(pattern=r'^r140-execution-task:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    execution_input_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    plan_id: str = Field(pattern=r'^multifidelity-plan:[0-9a-f]{64}$')
    plan_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    stage_id: str = Field(min_length=1)
    stage_order: int = Field(ge=0)
    candidate: MultiFidelityAuthorityRef
    evaluator_authority: MultiFidelityAuthorityRef

    execution_backend_ref: MultiFidelityAuthorityRef
    execution_configuration_ref: MultiFidelityAuthorityRef
    resource_estimate_ref: MultiFidelityAuthorityRef
    device_refs: tuple[MultiFidelityAuthorityRef, ...] = ()
    resource_request: ExecutionResourceVector

    @model_validator(mode='after')
    def validate_identity(self) -> 'MultiFidelityExecutionTask':
        device_keys = [item.key() for item in self.device_refs]
        if len(device_keys) != len(set(device_keys)):
            raise ValueError('execution device refs must be unique')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('MultiFidelityExecutionTask semantic hash mismatch')
        if self.execution_input_sha256 != expected:
            raise ValueError('execution input hash mismatch')
        if self.task_id != f'r140-execution-task:{expected}':
            raise ValueError('MultiFidelityExecutionTask id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={
                'task_id',
                'semantic_sha256',
                'execution_input_sha256',
            },
        )


class ExecutionTaskRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    task_id: str = Field(pattern=r'^r140-execution-task:[0-9a-f]{64}$')
    task_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    execution_input_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class ExecutionBatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    batch_index: int = Field(ge=0)
    task_refs: tuple[ExecutionTaskRef, ...] = Field(min_length=1)
    aggregate_resources: ExecutionResourceVector


class MultiFidelityExecutionSchedule(BaseModel):
    """Deterministic bounded batches preventing declared resource oversubscription."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = R140_EXECUTION_SCHEMA_VERSION
    authority_version: Literal[
        'r140-multifidelity-schedule-1'
    ] = R140_EXECUTION_SCHEDULE_AUTHORITY_VERSION
    scheduler_algorithm_version: Literal[
        'r140-bounded-batching-1'
    ] = R140_SCHEDULER_ALGORITHM_VERSION

    schedule_id: str = Field(pattern=r'^r140-execution-schedule:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    capacity_authority: ExecutionCapacityAuthority
    task_refs: tuple[ExecutionTaskRef, ...] = Field(min_length=1)
    batches: tuple[ExecutionBatch, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def validate_schedule(self) -> 'MultiFidelityExecutionSchedule':
        task_ids = [item.task_id for item in self.task_refs]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError('execution schedule task refs must be unique')
        batched = [
            item.task_id
            for batch in self.batches
            for item in batch.task_refs
        ]
        if tuple(batched) != tuple(task_ids):
            raise ValueError(
                'execution schedule batches must preserve exact task order/membership'
            )
        if [item.batch_index for item in self.batches] != list(
            range(len(self.batches))
        ):
            raise ValueError('execution batch indexes must be contiguous')
        for batch in self.batches:
            if not batch.aggregate_resources.fits_within(
                self.capacity_authority.capacity
            ):
                raise ValueError('execution batch exceeds declared capacity')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('MultiFidelityExecutionSchedule semantic hash mismatch')
        if self.schedule_id != f'r140-execution-schedule:{expected}':
            raise ValueError('MultiFidelityExecutionSchedule id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'schedule_id', 'semantic_sha256'},
        )


class MultiFidelityExecutionCacheEntry(BaseModel):
    """Exact reusable completed result for one exact execution input."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = R140_EXECUTION_SCHEMA_VERSION
    authority_version: Literal[
        'r140-multifidelity-cache-1'
    ] = R140_EXECUTION_CACHE_AUTHORITY_VERSION

    cache_entry_id: str = Field(
        pattern=r'^r140-execution-cache:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    task_id: str = Field(pattern=r'^r140-execution-task:[0-9a-f]{64}$')
    task_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    execution_input_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    result_authority_ref: MultiFidelityAuthorityRef
    execution_provenance_ref: MultiFidelityAuthorityRef
    completed_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_identity(self) -> 'MultiFidelityExecutionCacheEntry':
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('MultiFidelityExecutionCacheEntry semantic hash mismatch')
        if self.cache_entry_id != f'r140-execution-cache:{expected}':
            raise ValueError('MultiFidelityExecutionCacheEntry id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'cache_entry_id', 'semantic_sha256'},
        )


def build_multifidelity_execution_task(
    *,
    plan: MultiFidelityPlan,
    stage_id: str,
    candidate: MultiFidelityAuthorityRef,
    execution_backend_ref: MultiFidelityAuthorityRef,
    execution_configuration_ref: MultiFidelityAuthorityRef,
    resource_estimate_ref: MultiFidelityAuthorityRef,
    resource_request: ExecutionResourceVector,
    device_refs: Sequence[MultiFidelityAuthorityRef] = (),
) -> MultiFidelityExecutionTask:
    stage = plan.stage(stage_id)
    candidate_by_key = {item.key(): item for item in plan.candidates}
    exact_candidate = candidate_by_key.get(candidate.key())
    if exact_candidate is None or exact_candidate != candidate:
        raise ValueError('execution candidate is not an exact plan candidate')

    devices = tuple(sorted(
        (
            MultiFidelityAuthorityRef.model_validate(item.model_dump(mode='python'))
            for item in device_refs
        ),
        key=lambda item: item.key(),
    ))
    core = {
        'schema_version': R140_EXECUTION_SCHEMA_VERSION,
        'authority_version': R140_EXECUTION_TASK_AUTHORITY_VERSION,
        'plan_id': plan.plan_id,
        'plan_semantic_sha256': plan.semantic_sha256,
        'stage_id': stage.stage_id,
        'stage_order': stage.order,
        'candidate': candidate.model_dump(mode='json'),
        'evaluator_authority': stage.evaluator_authority.model_dump(mode='json'),
        'execution_backend_ref': execution_backend_ref.model_dump(mode='json'),
        'execution_configuration_ref': execution_configuration_ref.model_dump(
            mode='json'
        ),
        'resource_estimate_ref': resource_estimate_ref.model_dump(mode='json'),
        'device_refs': [item.model_dump(mode='json') for item in devices],
        'resource_request': resource_request.model_dump(mode='json'),
    }
    digest = _digest(core)
    return MultiFidelityExecutionTask(
        task_id=f'r140-execution-task:{digest}',
        semantic_sha256=digest,
        execution_input_sha256=digest,
        plan_id=plan.plan_id,
        plan_semantic_sha256=plan.semantic_sha256,
        stage_id=stage.stage_id,
        stage_order=stage.order,
        candidate=candidate,
        evaluator_authority=stage.evaluator_authority,
        execution_backend_ref=execution_backend_ref,
        execution_configuration_ref=execution_configuration_ref,
        resource_estimate_ref=resource_estimate_ref,
        device_refs=devices,
        resource_request=resource_request,
    )


def build_multifidelity_execution_schedule(
    *,
    tasks: Sequence[MultiFidelityExecutionTask],
    capacity_authority: ExecutionCapacityAuthority,
) -> MultiFidelityExecutionSchedule:
    ordered = tuple(sorted(
        (
            MultiFidelityExecutionTask.model_validate(item.model_dump(mode='python'))
            for item in tasks
        ),
        key=lambda item: (
            item.stage_order,
            item.candidate.authority_id,
            item.task_id,
        ),
    ))
    if not ordered:
        raise ValueError('execution schedule requires at least one task')
    ids = [item.task_id for item in ordered]
    if len(ids) != len(set(ids)):
        raise ValueError('execution schedule tasks must be unique')
    for task in ordered:
        if not task.resource_request.fits_within(capacity_authority.capacity):
            raise ValueError(
                f'execution task exceeds declared capacity: {task.task_id}'
            )

    batches: list[ExecutionBatch] = []
    current_tasks: list[ExecutionTaskRef] = []
    current_resources: ExecutionResourceVector | None = None

    def flush() -> None:
        nonlocal current_tasks, current_resources
        if not current_tasks or current_resources is None:
            return
        batches.append(
            ExecutionBatch(
                batch_index=len(batches),
                task_refs=tuple(current_tasks),
                aggregate_resources=current_resources,
            )
        )
        current_tasks = []
        current_resources = None

    for task in ordered:
        ref = ExecutionTaskRef(
            task_id=task.task_id,
            task_semantic_sha256=task.semantic_sha256,
            execution_input_sha256=task.execution_input_sha256,
        )
        if current_resources is None:
            current_tasks = [ref]
            current_resources = task.resource_request
            continue
        candidate_resources = current_resources.plus(task.resource_request)
        if candidate_resources.fits_within(capacity_authority.capacity):
            current_tasks.append(ref)
            current_resources = candidate_resources
        else:
            flush()
            current_tasks = [ref]
            current_resources = task.resource_request
    flush()

    task_refs = tuple(
        ref for batch in batches for ref in batch.task_refs
    )
    core = {
        'schema_version': R140_EXECUTION_SCHEMA_VERSION,
        'authority_version': R140_EXECUTION_SCHEDULE_AUTHORITY_VERSION,
        'scheduler_algorithm_version': R140_SCHEDULER_ALGORITHM_VERSION,
        'capacity_authority': capacity_authority.model_dump(mode='json'),
        'task_refs': [item.model_dump(mode='json') for item in task_refs],
        'batches': [item.model_dump(mode='json') for item in batches],
    }
    digest = _digest(core)
    return MultiFidelityExecutionSchedule(
        schedule_id=f'r140-execution-schedule:{digest}',
        semantic_sha256=digest,
        capacity_authority=capacity_authority,
        task_refs=task_refs,
        batches=tuple(batches),
    )


def build_multifidelity_execution_cache_entry(
    *,
    task: MultiFidelityExecutionTask,
    result_authority_ref: MultiFidelityAuthorityRef,
    execution_provenance_ref: MultiFidelityAuthorityRef,
    completed_at_utc: str,
) -> MultiFidelityExecutionCacheEntry:
    core = {
        'schema_version': R140_EXECUTION_SCHEMA_VERSION,
        'authority_version': R140_EXECUTION_CACHE_AUTHORITY_VERSION,
        'task_id': task.task_id,
        'task_semantic_sha256': task.semantic_sha256,
        'execution_input_sha256': task.execution_input_sha256,
        'result_authority_ref': result_authority_ref.model_dump(mode='json'),
        'execution_provenance_ref': execution_provenance_ref.model_dump(mode='json'),
        'completed_at_utc': completed_at_utc,
    }
    digest = _digest(core)
    return MultiFidelityExecutionCacheEntry(
        cache_entry_id=f'r140-execution-cache:{digest}',
        semantic_sha256=digest,
        task_id=task.task_id,
        task_semantic_sha256=task.semantic_sha256,
        execution_input_sha256=task.execution_input_sha256,
        result_authority_ref=result_authority_ref,
        execution_provenance_ref=execution_provenance_ref,
        completed_at_utc=completed_at_utc,
    )


class CadMultiFidelityExecutionRepository:
    """Append-only R140 execution identity, schedule and exact cache persistence."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        multifidelity_repository: MultiFidelityPlanResolver,
        external_authority_resolver: ExternalAuthorityResolver,
    ) -> None:
        self.scene_repository = scene_repository
        self.multifidelity_repository = multifidelity_repository
        self.external_authority_resolver = external_authority_resolver
        self.path = Path(scene_repository.path)
        if Path(multifidelity_repository.path) != self.path:
            raise ValueError(
                'R140 execution and multi-fidelity repositories must share '
                'one native CAD database'
            )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cad_r140_execution_tasks (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    execution_input_sha256 TEXT NOT NULL UNIQUE,
                    plan_id TEXT NOT NULL,
                    stage_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_r140_task_plan_stage_seq
                    ON cad_r140_execution_tasks(plan_id, stage_id, seq ASC);

                CREATE TABLE IF NOT EXISTS cad_r140_execution_schedules (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cad_r140_execution_cache (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    cache_entry_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    execution_input_sha256 TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL,
                    FOREIGN KEY(task_id)
                        REFERENCES cad_r140_execution_tasks(task_id)
                );
                """
            )

    def _resolve_external(
        self,
        ref: MultiFidelityAuthorityRef,
        *,
        label: str,
    ) -> MultiFidelityAuthorityRef:
        resolved = self.external_authority_resolver(ref)
        if resolved is None:
            raise ValueError(f'{label} exact external authority does not exist')
        if resolved != ref:
            raise ValueError(f'{label} exact external authority mismatch')
        return resolved

    def _validate_task(
        self,
        task: MultiFidelityExecutionTask,
    ) -> MultiFidelityExecutionTask:
        task = MultiFidelityExecutionTask.model_validate(
            task.model_dump(mode='python')
        )
        plan = self.multifidelity_repository.get_plan(task.plan_id)
        if plan is None:
            raise ValueError('R140 task references missing MultiFidelityPlan')
        if plan.semantic_sha256 != task.plan_semantic_sha256:
            raise ValueError('R140 task MultiFidelityPlan hash mismatch')
        stage = plan.stage(task.stage_id)
        if stage.order != task.stage_order:
            raise ValueError('R140 task stage order mismatch')
        if stage.evaluator_authority != task.evaluator_authority:
            raise ValueError('R140 task evaluator authority mismatch')
        if task.candidate not in plan.candidates:
            raise ValueError('R140 task candidate is not in exact plan')
        rebuilt = build_multifidelity_execution_task(
            plan=plan,
            stage_id=task.stage_id,
            candidate=task.candidate,
            execution_backend_ref=task.execution_backend_ref,
            execution_configuration_ref=task.execution_configuration_ref,
            resource_estimate_ref=task.resource_estimate_ref,
            device_refs=task.device_refs,
            resource_request=task.resource_request,
        )
        if rebuilt != task:
            raise ValueError('R140 task does not reproduce from exact authorities')

        for ref, label in (
            (task.evaluator_authority, 'stage evaluator'),
            (task.execution_backend_ref, 'execution backend'),
            (task.execution_configuration_ref, 'execution configuration'),
            (task.resource_estimate_ref, 'resource estimate'),
        ):
            self._resolve_external(ref, label=label)
        for ref in task.device_refs:
            self._resolve_external(ref, label='execution device')
        return task

    def save_task(
        self,
        task: MultiFidelityExecutionTask,
    ) -> MultiFidelityExecutionTask:
        task = self._validate_task(task)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_r140_execution_tasks
                WHERE task_id=?
                """,
                (task.task_id,),
            ).fetchone()
            if row is not None:
                persisted = MultiFidelityExecutionTask.model_validate_json(
                    row['payload_json']
                )
                if persisted != task:
                    raise ValueError(
                        'R140 execution task id exists with different semantics'
                    )
                return self._validate_task(persisted)
            connection.execute(
                """
                INSERT INTO cad_r140_execution_tasks(
                    task_id,
                    semantic_sha256,
                    execution_input_sha256,
                    plan_id,
                    stage_id,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.semantic_sha256,
                    task.execution_input_sha256,
                    task.plan_id,
                    task.stage_id,
                    task.model_dump_json(),
                    _utc_now(),
                ),
            )
        return task

    def get_task(
        self,
        task_id: str,
    ) -> MultiFidelityExecutionTask | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_r140_execution_tasks
                WHERE task_id=?
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate_task(
            MultiFidelityExecutionTask.model_validate_json(row['payload_json'])
        )

    def _validate_schedule(
        self,
        schedule: MultiFidelityExecutionSchedule,
    ) -> MultiFidelityExecutionSchedule:
        schedule = MultiFidelityExecutionSchedule.model_validate(
            schedule.model_dump(mode='python')
        )
        self._resolve_external(
            schedule.capacity_authority.capacity_authority_ref,
            label='execution capacity',
        )
        tasks: list[MultiFidelityExecutionTask] = []
        for ref in schedule.task_refs:
            task = self.get_task(ref.task_id)
            if task is None:
                raise ValueError('R140 schedule references missing task')
            if (
                task.semantic_sha256 != ref.task_semantic_sha256
                or task.execution_input_sha256 != ref.execution_input_sha256
            ):
                raise ValueError('R140 schedule task identity mismatch')
            tasks.append(task)
        rebuilt = build_multifidelity_execution_schedule(
            tasks=tasks,
            capacity_authority=schedule.capacity_authority,
        )
        if rebuilt != schedule:
            raise ValueError(
                'R140 schedule does not reproduce from exact tasks/capacity'
            )
        return schedule

    def save_schedule(
        self,
        schedule: MultiFidelityExecutionSchedule,
    ) -> MultiFidelityExecutionSchedule:
        schedule = self._validate_schedule(schedule)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_r140_execution_schedules
                WHERE schedule_id=?
                """,
                (schedule.schedule_id,),
            ).fetchone()
            if row is not None:
                persisted = MultiFidelityExecutionSchedule.model_validate_json(
                    row['payload_json']
                )
                if persisted != schedule:
                    raise ValueError(
                        'R140 schedule id exists with different semantics'
                    )
                return self._validate_schedule(persisted)
            connection.execute(
                """
                INSERT INTO cad_r140_execution_schedules(
                    schedule_id,
                    semantic_sha256,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    schedule.schedule_id,
                    schedule.semantic_sha256,
                    schedule.model_dump_json(),
                    _utc_now(),
                ),
            )
        return schedule

    def get_schedule(
        self,
        schedule_id: str,
    ) -> MultiFidelityExecutionSchedule | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_r140_execution_schedules
                WHERE schedule_id=?
                """,
                (schedule_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate_schedule(
            MultiFidelityExecutionSchedule.model_validate_json(
                row['payload_json']
            )
        )

    def _validate_cache_entry(
        self,
        entry: MultiFidelityExecutionCacheEntry,
    ) -> MultiFidelityExecutionCacheEntry:
        entry = MultiFidelityExecutionCacheEntry.model_validate(
            entry.model_dump(mode='python')
        )
        task = self.get_task(entry.task_id)
        if task is None:
            raise ValueError('R140 cache entry references missing task')
        if (
            task.semantic_sha256 != entry.task_semantic_sha256
            or task.execution_input_sha256 != entry.execution_input_sha256
        ):
            raise ValueError('R140 cache task/input identity mismatch')
        self._resolve_external(
            entry.result_authority_ref,
            label='execution result',
        )
        self._resolve_external(
            entry.execution_provenance_ref,
            label='execution provenance',
        )
        rebuilt = build_multifidelity_execution_cache_entry(
            task=task,
            result_authority_ref=entry.result_authority_ref,
            execution_provenance_ref=entry.execution_provenance_ref,
            completed_at_utc=entry.completed_at_utc,
        )
        if rebuilt != entry:
            raise ValueError('R140 cache entry does not reproduce exactly')
        return entry

    def save_cache_entry(
        self,
        entry: MultiFidelityExecutionCacheEntry,
    ) -> MultiFidelityExecutionCacheEntry:
        entry = self._validate_cache_entry(entry)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_r140_execution_cache
                WHERE cache_entry_id=?
                """,
                (entry.cache_entry_id,),
            ).fetchone()
            if row is not None:
                persisted = MultiFidelityExecutionCacheEntry.model_validate_json(
                    row['payload_json']
                )
                if persisted != entry:
                    raise ValueError(
                        'R140 cache entry id exists with different semantics'
                    )
                return self._validate_cache_entry(persisted)
            existing_input = connection.execute(
                """
                SELECT payload_json
                FROM cad_r140_execution_cache
                WHERE execution_input_sha256=?
                """,
                (entry.execution_input_sha256,),
            ).fetchone()
            if existing_input is not None:
                persisted = MultiFidelityExecutionCacheEntry.model_validate_json(
                    existing_input['payload_json']
                )
                if persisted != entry:
                    raise ValueError(
                        'R140 exact execution input already has a different '
                        'completed cache authority'
                    )
                return self._validate_cache_entry(persisted)
            connection.execute(
                """
                INSERT INTO cad_r140_execution_cache(
                    cache_entry_id,
                    semantic_sha256,
                    execution_input_sha256,
                    task_id,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.cache_entry_id,
                    entry.semantic_sha256,
                    entry.execution_input_sha256,
                    entry.task_id,
                    entry.model_dump_json(),
                    _utc_now(),
                ),
            )
        return entry

    def reusable_cache(
        self,
        task: MultiFidelityExecutionTask,
    ) -> MultiFidelityExecutionCacheEntry | None:
        task = self._validate_task(task)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_r140_execution_cache
                WHERE execution_input_sha256=?
                """,
                (task.execution_input_sha256,),
            ).fetchone()
        if row is None:
            return None
        entry = MultiFidelityExecutionCacheEntry.model_validate_json(
            row['payload_json']
        )
        return self._validate_cache_entry(entry)

    def partition_resume(
        self,
        tasks: Sequence[MultiFidelityExecutionTask],
    ) -> tuple[
        tuple[MultiFidelityExecutionCacheEntry, ...],
        tuple[MultiFidelityExecutionTask, ...],
    ]:
        reused: list[MultiFidelityExecutionCacheEntry] = []
        pending: list[MultiFidelityExecutionTask] = []
        for task in tasks:
            entry = self.reusable_cache(task)
            if entry is None:
                pending.append(task)
            else:
                reused.append(entry)
        return tuple(reused), tuple(pending)
