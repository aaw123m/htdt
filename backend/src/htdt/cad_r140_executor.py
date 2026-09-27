from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_multifidelity import MultiFidelityAuthorityRef
from .cad_multifidelity_execution import (
    CadMultiFidelityExecutionRepository,
    ExecutionResourceVector,
    MultiFidelityExecutionCacheEntry,
    MultiFidelityExecutionSchedule,
    MultiFidelityExecutionTask,
    build_multifidelity_execution_cache_entry,
)
from .cad_repository import SceneRepository
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
    connect_sqlite,
)
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest
from .clock import utc_now_iso as _utc_now


R140_EXECUTOR_SCHEMA_VERSION = 1
R140_RESOURCE_ESTIMATE_AUTHORITY_VERSION = 'r140-resource-estimate-1'
R140_EXECUTION_RESULT_AUTHORITY_VERSION = 'r140-execution-result-1'
R140_EXECUTION_ATTEMPT_AUTHORITY_VERSION = 'r140-execution-attempt-1'
R140_EXECUTOR_IMPLEMENTATION_VERSION = 'r140-bounded-thread-executor-1'
R140_SYNTHETIC_WORKER_VERSION = 'r140-deterministic-synthetic-worker-1'


class ResourceAdmissionError(ValueError):
    def __init__(self, message: str, *, state: Literal['REJECT', 'DEFER']) -> None:
        super().__init__(message)
        self.state = state


class ResourceQuantity(BaseModel):
    """One estimate quantity without treating unknown values as zero."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    state: Literal['KNOWN', 'UNKNOWN', 'UNAVAILABLE']
    value: int | None = Field(default=None, ge=0)
    unit: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_state(self) -> 'ResourceQuantity':
        if self.state == 'KNOWN' and self.value is None:
            raise ValueError('KNOWN resource quantity requires a value')
        if self.state != 'KNOWN' and self.value is not None:
            raise ValueError('unknown/unavailable resource quantity cannot carry a value')
        return self

    @classmethod
    def known(cls, value: int, unit: str) -> 'ResourceQuantity':
        return cls(state='KNOWN', value=value, unit=unit)

    @classmethod
    def unknown(cls, unit: str) -> 'ResourceQuantity':
        return cls(state='UNKNOWN', unit=unit)

    @classmethod
    def unavailable(cls, unit: str) -> 'ResourceQuantity':
        return cls(state='UNAVAILABLE', unit=unit)

    def require(self, label: str) -> int:
        if self.state == 'UNKNOWN':
            raise ResourceAdmissionError(
                f'{label} is UNKNOWN; execution must be deferred or explicitly re-estimated',
                state='DEFER',
            )
        if self.state == 'UNAVAILABLE':
            raise ResourceAdmissionError(
                f'{label} is UNAVAILABLE for this executor',
                state='REJECT',
            )
        assert self.value is not None
        return self.value


class CpuResourceEstimateRequest(BaseModel):
    """Solver-neutral inputs for the CPU baseline estimator.

    Values are declared/derived by an adapter-specific estimator upstream. The
    R140 core preserves their method and uncertainty; it does not infer solver
    physics.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    execution_backend_ref: MultiFidelityAuthorityRef
    execution_configuration_ref: MultiFidelityAuthorityRef
    logical_cpu_demand: ResourceQuantity
    physical_core_demand: ResourceQuantity
    inner_solver_threads: ResourceQuantity
    peak_memory_bytes: ResourceQuantity
    scratch_bytes: ResourceQuantity
    gpu_slots: ResourceQuantity = Field(
        default_factory=lambda: ResourceQuantity.known(0, 'slots')
    )
    vram_bytes: ResourceQuantity = Field(
        default_factory=lambda: ResourceQuantity.unavailable('bytes')
    )
    estimate_method: str = Field(min_length=1)
    estimate_method_version: str = Field(min_length=1)
    confidence: Literal['HIGH', 'MEDIUM', 'LOW', 'UNKNOWN']
    assumptions: tuple[str, ...] = ()


class ExecutionResourceEstimate(BaseModel):
    """Immutable R140 resource estimate authority."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = R140_EXECUTOR_SCHEMA_VERSION
    authority_version: Literal[
        'r140-resource-estimate-1'
    ] = R140_RESOURCE_ESTIMATE_AUTHORITY_VERSION
    estimate_id: str = Field(pattern=r'^r140-resource-estimate:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    execution_backend_ref: MultiFidelityAuthorityRef
    execution_configuration_ref: MultiFidelityAuthorityRef
    logical_cpu_demand: ResourceQuantity
    physical_core_demand: ResourceQuantity
    inner_solver_threads: ResourceQuantity
    peak_memory_bytes: ResourceQuantity
    scratch_bytes: ResourceQuantity
    gpu_slots: ResourceQuantity
    vram_bytes: ResourceQuantity
    estimate_method: str = Field(min_length=1)
    estimate_method_version: str = Field(min_length=1)
    confidence: Literal['HIGH', 'MEDIUM', 'LOW', 'UNKNOWN']
    assumptions: tuple[str, ...] = ()

    @model_validator(mode='after')
    def validate_identity(self) -> 'ExecutionResourceEstimate':
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('ExecutionResourceEstimate semantic hash mismatch')
        if self.estimate_id != f'r140-resource-estimate:{expected}':
            raise ValueError('ExecutionResourceEstimate id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'estimate_id', 'semantic_sha256'},
        )

    def authority_ref(self) -> MultiFidelityAuthorityRef:
        return MultiFidelityAuthorityRef(
            authority_kind='resource_estimate',
            authority_id=self.estimate_id,
            authority_version=self.authority_version,
            semantic_sha256=self.semantic_sha256,
        )

    def admission_resource_vector(self) -> ExecutionResourceVector:
        logical = self.logical_cpu_demand.require('logical CPU demand')
        inner = self.inner_solver_threads.require('inner solver thread count')
        memory = self.peak_memory_bytes.require('peak RAM estimate')
        scratch = self.scratch_bytes.require('scratch/disk estimate')
        gpu_slots = self.gpu_slots.require('GPU slot estimate')
        if logical <= 0:
            raise ResourceAdmissionError(
                'CPU baseline requires positive logical CPU demand',
                state='REJECT',
            )
        if inner <= 0:
            raise ResourceAdmissionError(
                'CPU baseline requires positive inner solver thread count',
                state='REJECT',
            )
        if inner > logical:
            raise ResourceAdmissionError(
                'inner solver threads exceed declared logical CPU demand',
                state='REJECT',
            )
        return ExecutionResourceVector(
            cpu_threads=logical,
            gpu_slots=gpu_slots,
            memory_bytes=memory,
            scratch_bytes=scratch,
        )


class DeclaredCpuResourceEstimator:
    """Bounded CPU-baseline estimator preserving explicit declared evidence."""

    def estimate(
        self,
        request: CpuResourceEstimateRequest,
    ) -> ExecutionResourceEstimate:
        core = {
            'schema_version': R140_EXECUTOR_SCHEMA_VERSION,
            'authority_version': R140_RESOURCE_ESTIMATE_AUTHORITY_VERSION,
            **request.model_dump(mode='json'),
        }
        digest = _digest(core)
        return ExecutionResourceEstimate(
            estimate_id=f'r140-resource-estimate:{digest}',
            semantic_sha256=digest,
            **request.model_dump(mode='python'),
        )


class RuntimeMetricEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    state: Literal['KNOWN', 'UNSUPPORTED']
    value: int | None = Field(default=None, ge=0)
    unit: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_state(self) -> 'RuntimeMetricEvidence':
        if self.state == 'KNOWN' and self.value is None:
            raise ValueError('KNOWN runtime metric requires a value')
        if self.state == 'UNSUPPORTED' and self.value is not None:
            raise ValueError('UNSUPPORTED runtime metric cannot carry a value')
        return self


class ExecutionWorkerAllocation(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    executor_implementation_version: Literal[
        'r140-bounded-thread-executor-1'
    ] = R140_EXECUTOR_IMPLEMENTATION_VERSION
    outer_worker_index: int = Field(ge=0)
    worker_identity: str = Field(min_length=1)
    process_id: int = Field(gt=0)
    thread_name: str = Field(min_length=1)
    logical_cpu_reserved: int = Field(gt=0)
    inner_solver_threads: int = Field(gt=0)
    memory_reserved_bytes: int = Field(ge=0)
    scratch_reserved_bytes: int = Field(ge=0)
    gpu_slots_reserved: int = Field(ge=0)


class ExecutionTelemetry(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    queued_at_utc: str = Field(min_length=1)
    started_at_utc: str | None = Field(default=None, min_length=1)
    finished_at_utc: str = Field(min_length=1)
    wall_time_seconds: float = Field(ge=0.0)
    task_state: Literal['SUCCEEDED', 'FAILED', 'CANCELLED']
    worker_allocation: ExecutionWorkerAllocation | None = None
    peak_memory: RuntimeMetricEvidence
    scratch_usage: RuntimeMetricEvidence
    cancellation_requested: bool
    cancellation_requested_at_utc: str | None = Field(default=None, min_length=1)
    failure_reason: str | None = Field(default=None, min_length=1)
    exit_condition: str = Field(min_length=1)
    partial_diagnostic: str | None = Field(default=None, min_length=1)
    last_progress_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    last_progress_message: str | None = Field(default=None, min_length=1)


class ExecutionWorkerOutput(BaseModel):
    """Opaque adapter output. R140 core never interprets acoustic physics."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    result_authority_ref: MultiFidelityAuthorityRef
    execution_provenance_ref: MultiFidelityAuthorityRef


class ExecutionWorkerPort(Protocol):
    def __call__(
        self,
        task: MultiFidelityExecutionTask,
        context: 'ExecutionInvocationContext',
    ) -> ExecutionWorkerOutput:
        ...


class ExecutionCancelled(RuntimeError):
    pass


class ExecutionWorkerFailure(RuntimeError):
    def __init__(
        self,
        reason: str,
        *,
        exit_condition: str = 'worker_failure',
        partial_diagnostic: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.exit_condition = exit_condition
        self.partial_diagnostic = partial_diagnostic


class ExecutionCancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._requested_at_utc: str | None = None
        self._completed = False

    @property
    def requested_at_utc(self) -> str | None:
        with self._lock:
            return self._requested_at_utc

    def request(self) -> bool:
        with self._lock:
            if self._event.is_set() or self._completed:
                return False
            self._requested_at_utc = _utc_now()
            self._event.set()
            return True

    def try_complete(self) -> bool:
        """Atomically win success vs cancellation before cache materialization."""
        with self._lock:
            if self._event.is_set():
                return False
            self._completed = True
            return True

    def is_requested(self) -> bool:
        return self._event.is_set()


ProgressSink = Callable[[str, float, str | None], None]


class ExecutionInvocationContext:
    """Runtime port passed to a solver adapter callback."""

    def __init__(
        self,
        *,
        task_id: str,
        token: ExecutionCancellationToken,
        allocation: ExecutionWorkerAllocation,
        progress_sink: ProgressSink | None,
    ) -> None:
        self.task_id = task_id
        self.token = token
        self.allocation = allocation
        self._progress_sink = progress_sink
        self._lock = threading.Lock()
        self._scratch_usage_bytes: int | None = None
        self._progress_fraction: float | None = None
        self._progress_message: str | None = None

    def is_cancelled(self) -> bool:
        return self.token.is_requested()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise ExecutionCancelled('execution cancellation requested')

    def report_progress(
        self,
        fraction: float,
        message: str | None = None,
    ) -> None:
        if fraction < 0.0 or fraction > 1.0:
            raise ValueError('progress fraction must be within [0, 1]')
        with self._lock:
            self._progress_fraction = float(fraction)
            self._progress_message = message
        if self._progress_sink is not None:
            self._progress_sink(self.task_id, float(fraction), message)

    def report_scratch_usage(self, bytes_used: int) -> None:
        if bytes_used < 0:
            raise ValueError('scratch usage must be non-negative')
        with self._lock:
            if (
                self._scratch_usage_bytes is None
                or bytes_used > self._scratch_usage_bytes
            ):
                self._scratch_usage_bytes = int(bytes_used)

    def snapshot(self) -> tuple[int | None, float | None, str | None]:
        with self._lock:
            return (
                self._scratch_usage_bytes,
                self._progress_fraction,
                self._progress_message,
            )


class ExecutionTaskResult(BaseModel):
    """Deterministic immutable success wrapper for one exact task/output."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = R140_EXECUTOR_SCHEMA_VERSION
    authority_version: Literal[
        'r140-execution-result-1'
    ] = R140_EXECUTION_RESULT_AUTHORITY_VERSION
    execution_result_id: str = Field(
        pattern=r'^r140-execution-result:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    task_id: str = Field(pattern=r'^r140-execution-task:[0-9a-f]{64}$')
    task_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    execution_input_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    result_authority_ref: MultiFidelityAuthorityRef
    execution_provenance_ref: MultiFidelityAuthorityRef

    @model_validator(mode='after')
    def validate_identity(self) -> 'ExecutionTaskResult':
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('ExecutionTaskResult semantic hash mismatch')
        if self.execution_result_id != f'r140-execution-result:{expected}':
            raise ValueError('ExecutionTaskResult id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'execution_result_id', 'semantic_sha256'},
        )


def build_execution_task_result(
    *,
    task: MultiFidelityExecutionTask,
    output: ExecutionWorkerOutput,
) -> ExecutionTaskResult:
    core = {
        'schema_version': R140_EXECUTOR_SCHEMA_VERSION,
        'authority_version': R140_EXECUTION_RESULT_AUTHORITY_VERSION,
        'task_id': task.task_id,
        'task_semantic_sha256': task.semantic_sha256,
        'execution_input_sha256': task.execution_input_sha256,
        'result_authority_ref': output.result_authority_ref.model_dump(mode='json'),
        'execution_provenance_ref': output.execution_provenance_ref.model_dump(
            mode='json'
        ),
    }
    digest = _digest(core)
    return ExecutionTaskResult(
        execution_result_id=f'r140-execution-result:{digest}',
        semantic_sha256=digest,
        task_id=task.task_id,
        task_semantic_sha256=task.semantic_sha256,
        execution_input_sha256=task.execution_input_sha256,
        result_authority_ref=output.result_authority_ref,
        execution_provenance_ref=output.execution_provenance_ref,
    )


class ExecutionAttemptRecord(BaseModel):
    """Immutable final runtime evidence for one executor attempt."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = R140_EXECUTOR_SCHEMA_VERSION
    authority_version: Literal[
        'r140-execution-attempt-1'
    ] = R140_EXECUTION_ATTEMPT_AUTHORITY_VERSION
    attempt_id: str = Field(pattern=r'^r140-execution-attempt:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    task_id: str = Field(pattern=r'^r140-execution-task:[0-9a-f]{64}$')
    task_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    execution_input_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    resource_estimate_ref: MultiFidelityAuthorityRef
    state: Literal['SUCCEEDED', 'FAILED', 'CANCELLED']
    execution_result: ExecutionTaskResult | None = None
    telemetry: ExecutionTelemetry

    @model_validator(mode='after')
    def validate_record(self) -> 'ExecutionAttemptRecord':
        if self.state != self.telemetry.task_state:
            raise ValueError('attempt state/telemetry state mismatch')
        if self.state == 'SUCCEEDED' and self.execution_result is None:
            raise ValueError('successful attempt requires execution result')
        if self.state != 'SUCCEEDED' and self.execution_result is not None:
            raise ValueError('failed/cancelled attempt cannot carry success result')
        if self.state == 'FAILED' and self.telemetry.failure_reason is None:
            raise ValueError('failed attempt requires failure reason')
        if self.state == 'CANCELLED' and not self.telemetry.cancellation_requested:
            raise ValueError('cancelled attempt requires cancellation request evidence')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('ExecutionAttemptRecord semantic hash mismatch')
        if self.attempt_id != f'r140-execution-attempt:{expected}':
            raise ValueError('ExecutionAttemptRecord id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'attempt_id', 'semantic_sha256'},
        )


def build_execution_attempt(
    *,
    task: MultiFidelityExecutionTask,
    estimate: ExecutionResourceEstimate,
    state: Literal['SUCCEEDED', 'FAILED', 'CANCELLED'],
    telemetry: ExecutionTelemetry,
    execution_result: ExecutionTaskResult | None = None,
) -> ExecutionAttemptRecord:
    core = {
        'schema_version': R140_EXECUTOR_SCHEMA_VERSION,
        'authority_version': R140_EXECUTION_ATTEMPT_AUTHORITY_VERSION,
        'task_id': task.task_id,
        'task_semantic_sha256': task.semantic_sha256,
        'execution_input_sha256': task.execution_input_sha256,
        'resource_estimate_ref': estimate.authority_ref().model_dump(mode='json'),
        'state': state,
        'execution_result': (
            execution_result.model_dump(mode='json')
            if execution_result is not None
            else None
        ),
        'telemetry': telemetry.model_dump(mode='json'),
    }
    digest = _digest(core)
    return ExecutionAttemptRecord(
        attempt_id=f'r140-execution-attempt:{digest}',
        semantic_sha256=digest,
        task_id=task.task_id,
        task_semantic_sha256=task.semantic_sha256,
        execution_input_sha256=task.execution_input_sha256,
        resource_estimate_ref=estimate.authority_ref(),
        state=state,
        execution_result=execution_result,
        telemetry=telemetry,
    )


class ExecutionRunSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    schedule_id: str
    reused_cache_entries: tuple[MultiFidelityExecutionCacheEntry, ...] = ()
    attempts: tuple[ExecutionAttemptRecord, ...] = ()

    @property
    def succeeded(self) -> tuple[ExecutionAttemptRecord, ...]:
        return tuple(item for item in self.attempts if item.state == 'SUCCEEDED')

    @property
    def failed(self) -> tuple[ExecutionAttemptRecord, ...]:
        return tuple(item for item in self.attempts if item.state == 'FAILED')

    @property
    def cancelled(self) -> tuple[ExecutionAttemptRecord, ...]:
        return tuple(item for item in self.attempts if item.state == 'CANCELLED')


class CadR140ExecutorRepository:
    """Append-only resource estimate, result and telemetry persistence."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        execution_repository: CadMultiFidelityExecutionRepository,
    ) -> None:
        self.scene_repository = scene_repository
        self.execution_repository = execution_repository
        self.path = Path(scene_repository.path)
        if Path(execution_repository.path) != self.path:
            raise ValueError(
                'R140 executor and execution repositories must share one native CAD database'
            )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_r140_resource_estimates', 'cad_r140_execution_results', 'cad_r140_execution_attempts')

    def save_resource_estimate(
        self,
        estimate: ExecutionResourceEstimate,
    ) -> ExecutionResourceEstimate:
        estimate = ExecutionResourceEstimate.model_validate(
            estimate.model_dump(mode='python')
        )
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_r140_resource_estimates
                WHERE estimate_id=?
                """,
                (estimate.estimate_id,),
            ).fetchone()
            if row is not None:
                persisted = ExecutionResourceEstimate.model_validate_json(
                    row['payload_json']
                )
                if persisted != estimate:
                    raise ValueError(
                        'R140 resource estimate id exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_r140_resource_estimates(
                    estimate_id,
                    semantic_sha256,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    estimate.estimate_id,
                    estimate.semantic_sha256,
                    estimate.model_dump_json(),
                    _utc_now(),
                ),
            )
        return estimate

    def get_resource_estimate(
        self,
        estimate_id: str,
    ) -> ExecutionResourceEstimate | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_r140_resource_estimates
                WHERE estimate_id=?
                """,
                (estimate_id,),
            ).fetchone()
        if row is None:
            return None
        return ExecutionResourceEstimate.model_validate_json(row['payload_json'])

    def _require_result_authorities(
        self,
        result: ExecutionTaskResult,
        *,
        label: str,
    ) -> ExecutionTaskResult:
        """Re-check task identity and exact external result/provenance refs."""
        task = self.execution_repository.get_task(result.task_id)
        if task is None:
            raise ValueError(f'{label} references missing task')
        if (
            task.semantic_sha256 != result.task_semantic_sha256
            or task.execution_input_sha256 != result.execution_input_sha256
        ):
            raise ValueError(f'{label} task/input identity mismatch')
        self.execution_repository._resolve_external(
            result.result_authority_ref,
            label='execution result',
        )
        self.execution_repository._resolve_external(
            result.execution_provenance_ref,
            label='execution provenance',
        )
        return result

    def _validate_result(self, result: ExecutionTaskResult) -> ExecutionTaskResult:
        result = ExecutionTaskResult.model_validate(result.model_dump(mode='python'))
        return self._require_result_authorities(result, label='R140 result')

    def save_result(self, result: ExecutionTaskResult) -> ExecutionTaskResult:
        result = self._validate_result(result)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            return self._save_result_in_transaction(connection, result)

    def _save_result_in_transaction(
        self,
        connection: sqlite3.Connection,
        result: ExecutionTaskResult,
    ) -> ExecutionTaskResult:
        """Persist one validated result inside the caller's transaction.

        The caller owns BEGIN/COMMIT/ROLLBACK and must have resolved the
        exact external authorities first; persisted rows were validated on
        commit.
        """
        row = connection.execute(
            """
            SELECT payload_json
            FROM cad_r140_execution_results
            WHERE execution_result_id=?
            """,
            (result.execution_result_id,),
        ).fetchone()
        if row is not None:
            persisted = ExecutionTaskResult.model_validate_json(
                row['payload_json']
            )
            if persisted != result:
                raise ValueError(
                    'R140 execution result id exists with different semantics'
                )
            return persisted
        connection.execute(
            """
            INSERT INTO cad_r140_execution_results(
                execution_result_id,
                semantic_sha256,
                task_id,
                payload_json,
                recorded_at_utc
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                result.execution_result_id,
                result.semantic_sha256,
                result.task_id,
                result.model_dump_json(),
                _utc_now(),
            ),
        )
        return result

    def get_result(self, execution_result_id: str) -> ExecutionTaskResult | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_r140_execution_results
                WHERE execution_result_id=?
                """,
                (execution_result_id,),
            ).fetchone()
        if row is None:
            return None
        result = ExecutionTaskResult.model_validate_json(row['payload_json'])
        return self._require_result_authorities(
            result,
            label='R140 persisted result',
        )

    def save_attempt(
        self,
        attempt: ExecutionAttemptRecord,
    ) -> ExecutionAttemptRecord:
        attempt = self._validate_attempt(attempt)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            return self._save_attempt_in_transaction(connection, attempt)

    def _validate_attempt(
        self,
        attempt: ExecutionAttemptRecord,
        *,
        committed_result: ExecutionTaskResult | None = None,
    ) -> ExecutionAttemptRecord:
        """Validate one attempt's exact task/estimate/result authorities.

        A SUCCEEDED attempt normally requires its embedded result to be
        persisted exactly. During atomic success publication the result is not
        committed yet, so ``committed_result`` is the co-committed row it must
        equal instead.
        """
        attempt = ExecutionAttemptRecord.model_validate(
            attempt.model_dump(mode='python')
        )
        task = self.execution_repository.get_task(attempt.task_id)
        if task is None:
            raise ValueError('R140 attempt references missing task')
        if (
            task.semantic_sha256 != attempt.task_semantic_sha256
            or task.execution_input_sha256 != attempt.execution_input_sha256
        ):
            raise ValueError('R140 attempt task/input identity mismatch')
        estimate = self.get_resource_estimate(
            attempt.resource_estimate_ref.authority_id
        )
        if estimate is None:
            raise ValueError('R140 attempt references missing resource estimate')
        if estimate.authority_ref() != attempt.resource_estimate_ref:
            raise ValueError('R140 attempt resource estimate mismatch')
        if task.resource_estimate_ref != attempt.resource_estimate_ref:
            raise ValueError('R140 attempt resource estimate is stale for task')
        if attempt.execution_result is not None:
            if committed_result is not None:
                if committed_result != attempt.execution_result:
                    raise ValueError(
                        'R140 attempt success result does not match the '
                        'co-committed result'
                    )
            else:
                persisted_result = self.get_result(
                    attempt.execution_result.execution_result_id
                )
                if persisted_result != attempt.execution_result:
                    raise ValueError(
                        'R140 attempt success result is not persisted exactly'
                    )
        return attempt

    def _save_attempt_in_transaction(
        self,
        connection: sqlite3.Connection,
        attempt: ExecutionAttemptRecord,
    ) -> ExecutionAttemptRecord:
        """Persist one validated attempt inside the caller's transaction.

        The caller owns BEGIN/COMMIT/ROLLBACK and must have validated all
        authorities first; persisted rows were validated on commit.
        """
        row = connection.execute(
            """
            SELECT payload_json
            FROM cad_r140_execution_attempts
            WHERE attempt_id=?
            """,
            (attempt.attempt_id,),
        ).fetchone()
        if row is not None:
            persisted = ExecutionAttemptRecord.model_validate_json(
                row['payload_json']
            )
            if persisted != attempt:
                raise ValueError(
                    'R140 execution attempt id exists with different semantics'
                )
            return persisted
        connection.execute(
            """
            INSERT INTO cad_r140_execution_attempts(
                attempt_id,
                semantic_sha256,
                task_id,
                state,
                payload_json,
                recorded_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                attempt.attempt_id,
                attempt.semantic_sha256,
                attempt.task_id,
                attempt.state,
                attempt.model_dump_json(),
                _utc_now(),
            ),
        )
        return attempt

    def commit_success(
        self,
        *,
        result: ExecutionTaskResult,
        cache_entry: MultiFidelityExecutionCacheEntry,
        attempt: ExecutionAttemptRecord,
    ) -> ExecutionAttemptRecord:
        """Validate then atomically publish one successful execution.

        Every exact authority — task/input identity, external
        result/provenance refs, resource estimate and cache/attempt coherence
        — is resolved before the shared write transaction begins. The runtime
        result, the reusable cache entry and the SUCCEEDED attempt then commit
        or roll back together, so a rejected authority or a mid-commit fault
        can never leave a committed result row behind.
        """
        result = self._validate_result(result)
        cache_entry = self.execution_repository._validate_cache_entry(cache_entry)
        if attempt.state != 'SUCCEEDED':
            raise ValueError(
                'R140 success publication requires a SUCCEEDED attempt'
            )
        attempt = self._validate_attempt(attempt, committed_result=result)
        if (
            cache_entry.task_id != result.task_id
            or cache_entry.result_authority_ref != result.result_authority_ref
            or cache_entry.execution_provenance_ref
            != result.execution_provenance_ref
        ):
            raise ValueError(
                'R140 success cache entry does not match the committed result'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            self._save_result_in_transaction(connection, result)
            self.execution_repository._save_cache_entry_in_transaction(
                connection,
                cache_entry,
            )
            persisted_attempt = self._save_attempt_in_transaction(
                connection,
                attempt,
            )
        return persisted_attempt

    def list_attempts(
        self,
        task_id: str,
    ) -> tuple[ExecutionAttemptRecord, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_r140_execution_attempts
                WHERE task_id=?
                ORDER BY seq ASC
                """,
                (task_id,),
            ).fetchall()
        attempts = tuple(
            ExecutionAttemptRecord.model_validate_json(row['payload_json'])
            for row in rows
        )
        for attempt in attempts:
            embedded = attempt.execution_result
            if embedded is None:
                continue
            if self.get_result(embedded.execution_result_id) != embedded:
                raise ValueError(
                    'R140 persisted attempt success result does not resolve '
                    'exactly'
                )
        return attempts


class BoundedR140Executor:
    """Actual bounded thread-worker executor for solver-neutral R140 tasks.

    Cancellation is cooperative for already-running callbacks. Queued futures
    are cancelled directly where possible. A callback that ignores the token
    may continue consuming CPU until it returns, but a cancellation request is
    re-checked before success/cache materialization so it can never become a
    success result after cancellation.
    """

    def __init__(
        self,
        *,
        execution_repository: CadMultiFidelityExecutionRepository,
        runtime_repository: CadR140ExecutorRepository,
        worker_port: ExecutionWorkerPort,
        max_workers: int,
        progress_sink: ProgressSink | None = None,
    ) -> None:
        if max_workers <= 0:
            raise ValueError('max_workers must be positive')
        if execution_repository.path != runtime_repository.path:
            raise ValueError('R140 executor repositories must share one database')
        self.execution_repository = execution_repository
        self.runtime_repository = runtime_repository
        self.worker_port = worker_port
        self.max_workers = int(max_workers)
        self.progress_sink = progress_sink
        self._pool = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix='htdt-r140',
        )
        self._lock = threading.Lock()
        self._tokens: dict[str, ExecutionCancellationToken] = {}
        self._futures: dict[str, Future[ExecutionAttemptRecord]] = {}
        self._closed = False

    def __enter__(self) -> 'BoundedR140Executor':
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError('R140 executor is closed')

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            token = self._tokens.get(task_id)
            if token is None:
                return False
            changed = token.request()
            future = self._futures.get(task_id)
            if future is not None:
                future.cancel()
            return changed

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            tokens = tuple(self._tokens.values())
        for token in tokens:
            token.request()
        self._pool.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            self._tokens.clear()
            self._futures.clear()

    def _task_estimate(
        self,
        task: MultiFidelityExecutionTask,
    ) -> ExecutionResourceEstimate:
        estimate = self.runtime_repository.get_resource_estimate(
            task.resource_estimate_ref.authority_id
        )
        if estimate is None:
            raise ValueError(
                f'R140 task references missing executor resource estimate: {task.task_id}'
            )
        if estimate.authority_ref() != task.resource_estimate_ref:
            raise ValueError('R140 task resource estimate exact authority mismatch')
        if estimate.execution_backend_ref != task.execution_backend_ref:
            raise ValueError('R140 resource estimate backend mismatch')
        if estimate.execution_configuration_ref != task.execution_configuration_ref:
            raise ValueError('R140 resource estimate configuration mismatch')
        if estimate.admission_resource_vector() != task.resource_request:
            raise ValueError('R140 task resource request does not match exact estimate')
        return estimate

    def _validated_tasks(
        self,
        schedule: MultiFidelityExecutionSchedule,
    ) -> tuple[
        tuple[MultiFidelityExecutionTask, ...],
        dict[str, ExecutionResourceEstimate],
    ]:
        persisted = self.execution_repository.get_schedule(schedule.schedule_id)
        if persisted is None:
            raise ValueError('R140 executor requires persisted exact schedule')
        if persisted != schedule:
            raise ValueError('R140 executor schedule authority mismatch')
        tasks: list[MultiFidelityExecutionTask] = []
        estimates: dict[str, ExecutionResourceEstimate] = {}
        for ref in schedule.task_refs:
            task = self.execution_repository.get_task(ref.task_id)
            if task is None:
                raise ValueError('R140 executor schedule references missing task')
            if (
                task.semantic_sha256 != ref.task_semantic_sha256
                or task.execution_input_sha256 != ref.execution_input_sha256
            ):
                raise ValueError('R140 executor task identity mismatch')
            estimate = self._task_estimate(task)
            tasks.append(task)
            estimates[task.task_id] = estimate
        return tuple(tasks), estimates

    def _allocation(
        self,
        *,
        estimate: ExecutionResourceEstimate,
        outer_worker_index: int,
    ) -> ExecutionWorkerAllocation:
        vector = estimate.admission_resource_vector()
        inner = estimate.inner_solver_threads.require('inner solver thread count')
        current = threading.current_thread()
        return ExecutionWorkerAllocation(
            outer_worker_index=outer_worker_index,
            worker_identity=f'pid:{os.getpid()}/thread:{current.name}',
            process_id=os.getpid(),
            thread_name=current.name,
            logical_cpu_reserved=vector.cpu_threads,
            inner_solver_threads=inner,
            memory_reserved_bytes=vector.memory_bytes,
            scratch_reserved_bytes=vector.scratch_bytes,
            gpu_slots_reserved=vector.gpu_slots,
        )

    @staticmethod
    def _runtime_metric(
        value: int | None,
        *,
        unit: str,
    ) -> RuntimeMetricEvidence:
        if value is None:
            return RuntimeMetricEvidence(state='UNSUPPORTED', unit=unit)
        return RuntimeMetricEvidence(state='KNOWN', value=value, unit=unit)

    def _invoke_task(
        self,
        *,
        task: MultiFidelityExecutionTask,
        estimate: ExecutionResourceEstimate,
        queued_at_utc: str,
        token: ExecutionCancellationToken,
        outer_worker_index: int,
    ) -> ExecutionAttemptRecord:
        started_monotonic = time.monotonic()
        started_at = _utc_now()
        allocation = self._allocation(
            estimate=estimate,
            outer_worker_index=outer_worker_index,
        )
        context = ExecutionInvocationContext(
            task_id=task.task_id,
            token=token,
            allocation=allocation,
            progress_sink=self.progress_sink,
        )

        def build_telemetry(
            *,
            state: Literal['SUCCEEDED', 'FAILED', 'CANCELLED'],
            finished_at_utc: str,
            exit_condition: str,
            failure_reason: str | None = None,
            partial_diagnostic: str | None = None,
        ) -> ExecutionTelemetry:
            scratch_usage, progress_fraction, progress_message = context.snapshot()
            return ExecutionTelemetry(
                queued_at_utc=queued_at_utc,
                started_at_utc=started_at,
                finished_at_utc=finished_at_utc,
                wall_time_seconds=max(0.0, time.monotonic() - started_monotonic),
                task_state=state,
                worker_allocation=allocation,
                peak_memory=RuntimeMetricEvidence(
                    state='UNSUPPORTED',
                    unit='bytes',
                ),
                scratch_usage=self._runtime_metric(
                    scratch_usage,
                    unit='bytes',
                ),
                cancellation_requested=token.is_requested(),
                cancellation_requested_at_utc=token.requested_at_utc,
                failure_reason=failure_reason,
                exit_condition=exit_condition,
                partial_diagnostic=partial_diagnostic,
                last_progress_fraction=progress_fraction,
                last_progress_message=progress_message,
            )

        state: Literal['SUCCEEDED', 'FAILED', 'CANCELLED']
        result: ExecutionTaskResult | None = None
        cache_entry: MultiFidelityExecutionCacheEntry | None = None
        failure_reason: str | None = None
        partial_diagnostic: str | None = None
        exit_condition = 'completed'

        try:
            context.raise_if_cancelled()
            output = self.worker_port(task, context)
            context.raise_if_cancelled()
            if not token.try_complete():
                raise ExecutionCancelled('execution cancellation requested')
            result = build_execution_task_result(task=task, output=output)
            finished_at = _utc_now()
            cache_entry = build_multifidelity_execution_cache_entry(
                task=task,
                result_authority_ref=output.result_authority_ref,
                execution_provenance_ref=output.execution_provenance_ref,
                completed_at_utc=finished_at,
            )
            state = 'SUCCEEDED'
        except ExecutionCancelled as exc:
            finished_at = _utc_now()
            state = 'CANCELLED'
            failure_reason = str(exc)
            exit_condition = 'cooperative_cancel'
        except ExecutionWorkerFailure as exc:
            finished_at = _utc_now()
            state = 'FAILED'
            failure_reason = exc.reason
            exit_condition = exc.exit_condition
            partial_diagnostic = exc.partial_diagnostic
        except Exception as exc:
            finished_at = _utc_now()
            state = 'FAILED'
            failure_reason = f'{type(exc).__name__}: {exc}'
            exit_condition = 'unhandled_worker_exception'

        attempt = build_execution_attempt(
            task=task,
            estimate=estimate,
            state=state,
            telemetry=build_telemetry(
                state=state,
                finished_at_utc=finished_at,
                exit_condition=exit_condition,
                failure_reason=failure_reason,
                partial_diagnostic=partial_diagnostic,
            ),
            execution_result=result if state == 'SUCCEEDED' else None,
        )
        if state == 'SUCCEEDED':
            # Publish the runtime result, the exact reusable cache entry and
            # the SUCCEEDED attempt atomically: a rejected external authority
            # or a mid-commit fault rolls back all three, so no committed
            # result row can survive as an orphan. A publication failure is
            # then recorded as an independent FAILED attempt.
            assert result is not None and cache_entry is not None
            try:
                return self.runtime_repository.commit_success(
                    result=result,
                    cache_entry=cache_entry,
                    attempt=attempt,
                )
            except Exception as exc:
                attempt = build_execution_attempt(
                    task=task,
                    estimate=estimate,
                    state='FAILED',
                    telemetry=build_telemetry(
                        state='FAILED',
                        finished_at_utc=_utc_now(),
                        exit_condition='result_publication_failure',
                        failure_reason=f'{type(exc).__name__}: {exc}',
                    ),
                )
        return self.runtime_repository.save_attempt(attempt)

    def _record_queued_cancel(
        self,
        *,
        task: MultiFidelityExecutionTask,
        estimate: ExecutionResourceEstimate,
        queued_at_utc: str,
        token: ExecutionCancellationToken,
    ) -> ExecutionAttemptRecord:
        if not token.is_requested():
            token.request()
        telemetry = ExecutionTelemetry(
            queued_at_utc=queued_at_utc,
            started_at_utc=None,
            finished_at_utc=_utc_now(),
            wall_time_seconds=0.0,
            task_state='CANCELLED',
            worker_allocation=None,
            peak_memory=RuntimeMetricEvidence(
                state='UNSUPPORTED',
                unit='bytes',
            ),
            scratch_usage=RuntimeMetricEvidence(
                state='UNSUPPORTED',
                unit='bytes',
            ),
            cancellation_requested=True,
            cancellation_requested_at_utc=token.requested_at_utc,
            failure_reason='execution cancelled before worker start',
            exit_condition='queued_cancel',
        )
        attempt = build_execution_attempt(
            task=task,
            estimate=estimate,
            state='CANCELLED',
            telemetry=telemetry,
        )
        return self.runtime_repository.save_attempt(attempt)

    def run_schedule(
        self,
        schedule: MultiFidelityExecutionSchedule,
    ) -> ExecutionRunSummary:
        self._ensure_open()
        tasks, estimates = self._validated_tasks(schedule)
        reused, pending = self.execution_repository.partition_resume(tasks)
        pending_by_id = {task.task_id: task for task in pending}
        attempts_by_id: dict[str, ExecutionAttemptRecord] = {}

        batch_by_task: dict[str, int] = {}
        for batch in schedule.batches:
            for ref in batch.task_refs:
                batch_by_task[ref.task_id] = batch.batch_index

        for batch in schedule.batches:
            batch_tasks = [
                pending_by_id[ref.task_id]
                for ref in batch.task_refs
                if ref.task_id in pending_by_id
            ]
            if not batch_tasks:
                continue

            futures: list[
                tuple[
                    MultiFidelityExecutionTask,
                    ExecutionResourceEstimate,
                    str,
                    ExecutionCancellationToken,
                    Future[ExecutionAttemptRecord],
                ]
            ] = []
            prepared: list[
                tuple[
                    int,
                    MultiFidelityExecutionTask,
                    ExecutionResourceEstimate,
                    str,
                    ExecutionCancellationToken,
                ]
            ] = []
            for index, task in enumerate(batch_tasks):
                prepared.append(
                    (
                        index,
                        task,
                        estimates[task.task_id],
                        _utc_now(),
                        ExecutionCancellationToken(),
                    )
                )

            # Register the whole bounded batch before any worker can begin.
            # This makes every queued task immediately cancellable even when the
            # first submitted worker starts on another thread without delay.
            with self._lock:
                if self._closed:
                    raise RuntimeError('R140 executor closed during schedule execution')
                for _, task, _, _, token in prepared:
                    self._tokens[task.task_id] = token

            for index, task, estimate, queued_at, token in prepared:
                with self._lock:
                    if token.is_requested():
                        future = None
                    else:
                        future = self._pool.submit(
                            self._invoke_task,
                            task=task,
                            estimate=estimate,
                            queued_at_utc=queued_at,
                            token=token,
                            outer_worker_index=index % self.max_workers,
                        )
                        self._futures[task.task_id] = future
                if future is None:
                    attempts_by_id[task.task_id] = self._record_queued_cancel(
                        task=task,
                        estimate=estimate,
                        queued_at_utc=queued_at,
                        token=token,
                    )
                    with self._lock:
                        self._tokens.pop(task.task_id, None)
                    continue
                futures.append((task, estimate, queued_at, token, future))

            for task, estimate, queued_at, token, future in futures:
                try:
                    attempt = future.result()
                except CancelledError:
                    attempt = self._record_queued_cancel(
                        task=task,
                        estimate=estimate,
                        queued_at_utc=queued_at,
                        token=token,
                    )
                finally:
                    with self._lock:
                        self._tokens.pop(task.task_id, None)
                        self._futures.pop(task.task_id, None)
                attempts_by_id[task.task_id] = attempt

        ordered_attempts = tuple(
            attempts_by_id[ref.task_id]
            for ref in schedule.task_refs
            if ref.task_id in attempts_by_id
        )
        return ExecutionRunSummary(
            schedule_id=schedule.schedule_id,
            reused_cache_entries=reused,
            attempts=ordered_attempts,
        )


SyntheticRefRegistrar = Callable[[MultiFidelityAuthorityRef], None]


class DeterministicSyntheticWorker:
    """CI-only deterministic worker; never acoustic production evidence."""

    def __init__(
        self,
        *,
        fail_task_ids: Sequence[str] = (),
        steps: int = 1,
        step_delay_seconds: float = 0.0,
        register_ref: SyntheticRefRegistrar | None = None,
    ) -> None:
        if steps <= 0:
            raise ValueError('synthetic worker steps must be positive')
        if step_delay_seconds < 0.0:
            raise ValueError('synthetic worker delay must be non-negative')
        self.fail_task_ids = frozenset(fail_task_ids)
        self.steps = int(steps)
        self.step_delay_seconds = float(step_delay_seconds)
        self.register_ref = register_ref

    def __call__(
        self,
        task: MultiFidelityExecutionTask,
        context: ExecutionInvocationContext,
    ) -> ExecutionWorkerOutput:
        for index in range(self.steps):
            context.raise_if_cancelled()
            context.report_progress(
                (index + 1) / self.steps,
                'synthetic execution',
            )
            if self.step_delay_seconds:
                time.sleep(self.step_delay_seconds)
        context.raise_if_cancelled()

        if task.task_id in self.fail_task_ids:
            raise ExecutionWorkerFailure(
                'synthetic requested failure',
                exit_condition='synthetic_failure',
                partial_diagnostic='deterministic synthetic diagnostic',
            )

        context.report_scratch_usage(task.resource_request.scratch_bytes // 2)
        result_digest = _digest(
            {
                'worker_version': R140_SYNTHETIC_WORKER_VERSION,
                'task_id': task.task_id,
                'execution_input_sha256': task.execution_input_sha256,
                'kind': 'result',
            }
        )
        provenance_digest = _digest(
            {
                'worker_version': R140_SYNTHETIC_WORKER_VERSION,
                'task_id': task.task_id,
                'execution_input_sha256': task.execution_input_sha256,
                'kind': 'provenance',
            }
        )
        result_ref = MultiFidelityAuthorityRef(
            authority_kind='synthetic_execution_result',
            authority_id=f'synthetic-result:{result_digest}',
            authority_version=R140_SYNTHETIC_WORKER_VERSION,
            semantic_sha256=result_digest,
        )
        provenance_ref = MultiFidelityAuthorityRef(
            authority_kind='synthetic_execution_provenance',
            authority_id=f'synthetic-provenance:{provenance_digest}',
            authority_version=R140_SYNTHETIC_WORKER_VERSION,
            semantic_sha256=provenance_digest,
        )
        if self.register_ref is not None:
            self.register_ref(result_ref)
            self.register_ref(provenance_ref)
        return ExecutionWorkerOutput(
            result_authority_ref=result_ref,
            execution_provenance_ref=provenance_ref,
        )
