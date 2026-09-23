from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from htdt.cad_multifidelity import (
    CadMultiFidelityRepository,
    MultiFidelityAuthorityRef,
    MultiFidelityStageDefinition,
    build_multifidelity_plan,
)
from htdt.cad_multifidelity_execution import (
    CadMultiFidelityExecutionRepository,
    ExecutionCapacityAuthority,
    build_multifidelity_execution_cache_entry,
    build_multifidelity_execution_schedule,
    build_multifidelity_execution_task,
)
from htdt.cad_r140_executor import (
    BoundedR140Executor,
    CadR140ExecutorRepository,
    CpuResourceEstimateRequest,
    DeclaredCpuResourceEstimator,
    DeterministicSyntheticWorker,
    ExecutionInvocationContext,
    ExecutionWorkerFailure,
    ExecutionWorkerOutput,
    ResourceAdmissionError,
    ResourceQuantity,
    build_execution_task_result,
)
from htdt.cad_repository import SceneRepository


def _ref(
    kind: str,
    authority_id: str,
    char: str,
    *,
    evaluator: bool = False,
) -> MultiFidelityAuthorityRef:
    kwargs = {}
    if evaluator:
        kwargs.update(
            evaluator_id=f'evaluator:{authority_id}',
            evaluator_version='1',
            model_id=f'model:{authority_id}',
            model_version='1',
            fidelity='fixture',
        )
    return MultiFidelityAuthorityRef(
        authority_kind=kind,
        authority_id=authority_id,
        authority_version='1',
        semantic_sha256=char * 64,
        **kwargs,
    )


def _plan(candidate_count: int = 3):
    candidates = tuple(
        _ref('system_variant', f'variant-{index}', chr(ord('a') + index))
        for index in range(max(2, candidate_count))
    )
    stages = (
        MultiFidelityStageDefinition(
            stage_id='cpu',
            order=0,
            name='CPU baseline',
            policy='hard_gate',
            fidelity_label='fixture',
            evaluator_authority=_ref(
                'objective_evaluator',
                'cpu-evaluator',
                'd',
                evaluator=True,
            ),
        ),
        MultiFidelityStageDefinition(
            stage_id='final',
            order=1,
            name='Final common fidelity',
            policy='final_common_fidelity',
            fidelity_label='fixture-final',
            evaluator_authority=_ref(
                'objective_evaluator',
                'final-evaluator',
                '8',
                evaluator=True,
            ),
        ),
    )
    return build_multifidelity_plan(
        domain='o90_robustness',
        name='R140 executor fixture',
        baseline_authority=_ref('scene_revision', 'baseline', 'e'),
        candidates=candidates,
        stages=stages,
    )


def _plan_authority_refs(plan):
    refs = [plan.baseline_authority, *plan.candidates]
    for stage in plan.stages:
        refs.append(stage.evaluator_authority)
        if stage.validated_screening_relationship_ref is not None:
            refs.append(stage.validated_screening_relationship_ref)
    return tuple(refs)


class Fixture:
    def __init__(self, tmp_path: Path, *, candidate_count: int = 3) -> None:
        self.scene = SceneRepository(tmp_path / 'cad.sqlite3')
        self.authorities: dict[tuple[str, str, str | None, str], MultiFidelityAuthorityRef] = {}

        def resolve(ref: MultiFidelityAuthorityRef, context=None):
            return self.authorities.get(ref.key())

        self.resolve = resolve
        self.multifidelity = CadMultiFidelityRepository(
            self.scene,
            authority_resolver=resolve,
            stage_evidence_resolver=resolve,
        )
        self.plan = _plan(candidate_count)
        for ref in _plan_authority_refs(self.plan):
            self.register(ref)
        self.multifidelity.save_plan(self.plan)
        self.execution = CadMultiFidelityExecutionRepository(
            self.scene,
            multifidelity_repository=self.multifidelity,
            external_authority_resolver=self.resolve,
        )
        self.runtime = CadR140ExecutorRepository(
            self.scene,
            execution_repository=self.execution,
        )
        self.estimator = DeclaredCpuResourceEstimator()
        self.tasks = []

    def register(self, ref: MultiFidelityAuthorityRef) -> None:
        self.authorities[ref.key()] = ref

    def add_task(
        self,
        candidate_index: int,
        *,
        logical_cpu: int = 1,
        inner_threads: int = 1,
        config_char: str | None = None,
    ):
        candidate = self.plan.candidates[candidate_index]
        suffix = config_char or str(candidate_index + 1)
        backend = _ref('execution_backend', 'cpu-thread-worker', 'f')
        config = _ref(
            'execution_configuration',
            f'cpu-config-{suffix}',
            suffix,
        )
        estimate = self.estimator.estimate(
            CpuResourceEstimateRequest(
                execution_backend_ref=backend,
                execution_configuration_ref=config,
                logical_cpu_demand=ResourceQuantity.known(
                    logical_cpu, 'logical_cpus'
                ),
                physical_core_demand=ResourceQuantity.unknown('cores'),
                inner_solver_threads=ResourceQuantity.known(
                    inner_threads, 'threads'
                ),
                peak_memory_bytes=ResourceQuantity.known(1_000_000, 'bytes'),
                scratch_bytes=ResourceQuantity.known(100_000, 'bytes'),
                gpu_slots=ResourceQuantity.known(0, 'slots'),
                vram_bytes=ResourceQuantity.unavailable('bytes'),
                estimate_method='fixture-declared',
                estimate_method_version='1',
                confidence='HIGH',
                assumptions=('CI fixture only',),
            )
        )
        self.register(self.plan.stages[0].evaluator_authority)
        self.register(backend)
        self.register(config)
        self.register(estimate.authority_ref())
        self.runtime.save_resource_estimate(estimate)
        task = build_multifidelity_execution_task(
            plan=self.plan,
            stage_id='cpu',
            candidate=candidate,
            execution_backend_ref=backend,
            execution_configuration_ref=config,
            resource_estimate_ref=estimate.authority_ref(),
            resource_request=estimate.admission_resource_vector(),
        )
        self.execution.save_task(task)
        self.tasks.append(task)
        return task, estimate

    def schedule(self, *, cpu_capacity: int):
        capacity_ref = _ref('hardware_capacity', 'cpu-capacity', '9')
        self.register(capacity_ref)
        schedule = build_multifidelity_execution_schedule(
            tasks=tuple(self.tasks),
            capacity_authority=ExecutionCapacityAuthority(
                capacity_authority_ref=capacity_ref,
                capacity=self.tasks[0].resource_request.model_copy(
                    update={
                        'cpu_threads': cpu_capacity,
                        'memory_bytes': 10_000_000,
                        'scratch_bytes': 10_000_000,
                    }
                ),
            ),
        )
        self.execution.save_schedule(schedule)
        return schedule


def test_cpu_resource_estimate_preserves_unknown_and_rejects_unsafe_admission() -> None:
    backend = _ref('execution_backend', 'cpu', 'a')
    config = _ref('execution_configuration', 'config', 'b')
    estimator = DeclaredCpuResourceEstimator()

    unknown_memory = estimator.estimate(
        CpuResourceEstimateRequest(
            execution_backend_ref=backend,
            execution_configuration_ref=config,
            logical_cpu_demand=ResourceQuantity.known(2, 'logical_cpus'),
            physical_core_demand=ResourceQuantity.unknown('cores'),
            inner_solver_threads=ResourceQuantity.known(2, 'threads'),
            peak_memory_bytes=ResourceQuantity.unknown('bytes'),
            scratch_bytes=ResourceQuantity.known(0, 'bytes'),
            estimate_method='declared',
            estimate_method_version='1',
            confidence='LOW',
        )
    )
    assert unknown_memory.peak_memory_bytes.state == 'UNKNOWN'
    with pytest.raises(ResourceAdmissionError) as exc_info:
        unknown_memory.admission_resource_vector()
    assert exc_info.value.state == 'DEFER'

    oversubscribed = estimator.estimate(
        CpuResourceEstimateRequest(
            execution_backend_ref=backend,
            execution_configuration_ref=config,
            logical_cpu_demand=ResourceQuantity.known(2, 'logical_cpus'),
            physical_core_demand=ResourceQuantity.unknown('cores'),
            inner_solver_threads=ResourceQuantity.known(8, 'threads'),
            peak_memory_bytes=ResourceQuantity.known(1, 'bytes'),
            scratch_bytes=ResourceQuantity.known(0, 'bytes'),
            estimate_method='declared',
            estimate_method_version='1',
            confidence='HIGH',
        )
    )
    with pytest.raises(ResourceAdmissionError) as exc_info:
        oversubscribed.admission_resource_vector()
    assert exc_info.value.state == 'REJECT'


def test_serial_execution_cache_hit_and_deterministic_result_identity(
    tmp_path: Path,
) -> None:
    fx = Fixture(tmp_path, candidate_count=1)
    task, _ = fx.add_task(0)
    schedule = fx.schedule(cpu_capacity=1)
    worker = DeterministicSyntheticWorker(register_ref=fx.register)

    with BoundedR140Executor(
        execution_repository=fx.execution,
        runtime_repository=fx.runtime,
        worker_port=worker,
        max_workers=1,
    ) as executor:
        first = executor.run_schedule(schedule)
        second = executor.run_schedule(schedule)

    assert len(first.succeeded) == 1
    assert not first.failed
    assert not first.cancelled
    assert first.succeeded[0].execution_result is not None
    assert first.succeeded[0].execution_result.task_id == task.task_id
    assert len(second.attempts) == 0
    assert len(second.reused_cache_entries) == 1
    assert (
        second.reused_cache_entries[0].execution_input_sha256
        == task.execution_input_sha256
    )


def test_bounded_parallel_execution_respects_cpu_batching(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, candidate_count=3)
    for index in range(3):
        fx.add_task(index, logical_cpu=2, inner_threads=2)
    schedule = fx.schedule(cpu_capacity=4)
    assert [len(batch.task_refs) for batch in schedule.batches] == [2, 1]

    lock = threading.Lock()
    active = 0
    max_active = 0

    class TrackingWorker:
        def __call__(
            self,
            task,
            context: ExecutionInvocationContext,
        ) -> ExecutionWorkerOutput:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.03)
                return DeterministicSyntheticWorker(
                    register_ref=fx.register
                )(task, context)
            finally:
                with lock:
                    active -= 1

    with BoundedR140Executor(
        execution_repository=fx.execution,
        runtime_repository=fx.runtime,
        worker_port=TrackingWorker(),
        max_workers=8,
    ) as executor:
        summary = executor.run_schedule(schedule)

    assert len(summary.succeeded) == 3
    assert max_active == 2
    assert all(
        attempt.telemetry.worker_allocation is not None
        and attempt.telemetry.worker_allocation.inner_solver_threads == 2
        for attempt in summary.attempts
    )


def test_queued_cancellation_never_materializes_success(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, candidate_count=2)
    first, _ = fx.add_task(0)
    second, _ = fx.add_task(1)
    schedule = fx.schedule(cpu_capacity=2)
    started = threading.Event()
    release = threading.Event()

    class BlockingWorker:
        def __call__(self, task, context):
            if task.task_id == first.task_id:
                started.set()
                assert release.wait(2.0)
            return DeterministicSyntheticWorker(register_ref=fx.register)(
                task, context
            )

    executor = BoundedR140Executor(
        execution_repository=fx.execution,
        runtime_repository=fx.runtime,
        worker_port=BlockingWorker(),
        max_workers=1,
    )
    box = {}

    def run():
        box['summary'] = executor.run_schedule(schedule)

    runner = threading.Thread(target=run)
    runner.start()
    assert started.wait(2.0)
    assert executor.cancel(second.task_id)
    release.set()
    runner.join(10.0)
    assert not runner.is_alive()
    executor.close()

    summary = box['summary']
    cancelled = [item for item in summary.attempts if item.task_id == second.task_id][0]
    assert cancelled.state == 'CANCELLED'
    assert cancelled.telemetry.started_at_utc is None
    assert fx.execution.reusable_cache(second) is None


def test_running_cancellation_is_cooperative_and_blocks_success(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, candidate_count=1)
    task, _ = fx.add_task(0)
    schedule = fx.schedule(cpu_capacity=1)
    started = threading.Event()

    class CooperativeWorker:
        def __call__(self, task, context):
            started.set()
            while True:
                context.raise_if_cancelled()
                time.sleep(0.005)

    executor = BoundedR140Executor(
        execution_repository=fx.execution,
        runtime_repository=fx.runtime,
        worker_port=CooperativeWorker(),
        max_workers=1,
    )
    box = {}

    def run():
        box['summary'] = executor.run_schedule(schedule)

    runner = threading.Thread(target=run)
    runner.start()
    assert started.wait(2.0)
    assert executor.cancel(task.task_id)
    runner.join(3.0)
    executor.close()

    summary = box['summary']
    assert len(summary.cancelled) == 1
    assert summary.cancelled[0].telemetry.started_at_utc is not None
    assert summary.cancelled[0].telemetry.exit_condition == 'cooperative_cancel'
    assert fx.execution.reusable_cache(task) is None


def test_failure_retains_reason_runtime_and_partial_diagnostic(
    tmp_path: Path,
) -> None:
    fx = Fixture(tmp_path, candidate_count=1)
    task, _ = fx.add_task(0)
    schedule = fx.schedule(cpu_capacity=1)

    class FailingWorker:
        def __call__(self, task, context):
            context.report_progress(0.25, 'before failure')
            raise ExecutionWorkerFailure(
                'fixture worker failed',
                exit_condition='fixture_exit_17',
                partial_diagnostic='stderr tail',
            )

    with BoundedR140Executor(
        execution_repository=fx.execution,
        runtime_repository=fx.runtime,
        worker_port=FailingWorker(),
        max_workers=1,
    ) as executor:
        summary = executor.run_schedule(schedule)

    assert len(summary.failed) == 1
    attempt = summary.failed[0]
    assert attempt.task_id == task.task_id
    assert attempt.telemetry.failure_reason == 'fixture worker failed'
    assert attempt.telemetry.exit_condition == 'fixture_exit_17'
    assert attempt.telemetry.partial_diagnostic == 'stderr tail'
    assert attempt.telemetry.wall_time_seconds >= 0.0
    assert attempt.telemetry.last_progress_fraction == 0.25
    assert fx.execution.reusable_cache(task) is None


def test_resume_runs_only_unfinished_task(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, candidate_count=2)
    first, _ = fx.add_task(0)
    second, _ = fx.add_task(1)
    schedule = fx.schedule(cpu_capacity=2)

    with BoundedR140Executor(
        execution_repository=fx.execution,
        runtime_repository=fx.runtime,
        worker_port=DeterministicSyntheticWorker(
            fail_task_ids=(second.task_id,),
            register_ref=fx.register,
        ),
        max_workers=2,
    ) as executor:
        first_run = executor.run_schedule(schedule)

    assert {item.task_id for item in first_run.succeeded} == {first.task_id}
    assert {item.task_id for item in first_run.failed} == {second.task_id}

    with BoundedR140Executor(
        execution_repository=fx.execution,
        runtime_repository=fx.runtime,
        worker_port=DeterministicSyntheticWorker(register_ref=fx.register),
        max_workers=2,
    ) as executor:
        resumed = executor.run_schedule(schedule)

    assert {item.task_id for item in resumed.attempts} == {second.task_id}
    assert len(resumed.reused_cache_entries) == 1
    assert resumed.reused_cache_entries[0].task_id == first.task_id


def test_backend_or_config_change_is_exact_cache_miss(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, candidate_count=1)
    original, _ = fx.add_task(0, config_char='1')
    schedule = fx.schedule(cpu_capacity=1)
    with BoundedR140Executor(
        execution_repository=fx.execution,
        runtime_repository=fx.runtime,
        worker_port=DeterministicSyntheticWorker(register_ref=fx.register),
        max_workers=1,
    ) as executor:
        executor.run_schedule(schedule)

    changed, _ = fx.add_task(0, config_char='2')
    assert changed.execution_input_sha256 != original.execution_input_sha256
    assert fx.execution.reusable_cache(changed) is None


def test_stale_authority_rejected_before_worker_invocation(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, candidate_count=1)
    task, _ = fx.add_task(0)
    schedule = fx.schedule(cpu_capacity=1)
    fx.authorities.pop(task.execution_backend_ref.key())

    called = False

    class NeverWorker:
        def __call__(self, task, context):
            nonlocal called
            called = True
            raise AssertionError('must not run')

    with BoundedR140Executor(
        execution_repository=fx.execution,
        runtime_repository=fx.runtime,
        worker_port=NeverWorker(),
        max_workers=1,
    ) as executor:
        with pytest.raises(
            ValueError,
            match='execution backend exact external authority does not exist',
        ):
            executor.run_schedule(schedule)

    assert not called


def test_shutdown_cleanup_is_idempotent_and_releases_database(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, candidate_count=1)
    fx.add_task(0)
    schedule = fx.schedule(cpu_capacity=1)
    executor = BoundedR140Executor(
        execution_repository=fx.execution,
        runtime_repository=fx.runtime,
        worker_port=DeterministicSyntheticWorker(register_ref=fx.register),
        max_workers=1,
    )
    executor.run_schedule(schedule)
    executor.close()
    executor.close()

    with closing(sqlite3.connect(fx.scene.path)) as connection, connection:
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'

    moved = Path(str(fx.scene.path) + '.moved')
    Path(fx.scene.path).rename(moved)
    moved.rename(fx.scene.path)


def _row_counts(path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            table: connection.execute(
                f'SELECT COUNT(*) FROM {table}'
            ).fetchone()[0]
            for table in (
                'cad_r140_execution_results',
                'cad_r140_execution_cache',
                'cad_r140_execution_attempts',
            )
        }


def _failed_publication_counts() -> dict[str, int]:
    return {
        'cad_r140_execution_results': 0,
        'cad_r140_execution_cache': 0,
        'cad_r140_execution_attempts': 1,
    }


def test_unresolvable_worker_authorities_publish_no_success_rows(
    tmp_path: Path,
) -> None:
    fx = Fixture(tmp_path, candidate_count=1)
    task, _ = fx.add_task(0)
    schedule = fx.schedule(cpu_capacity=1)

    class PhantomRefWorker:
        def __call__(self, task, context) -> ExecutionWorkerOutput:
            return ExecutionWorkerOutput(
                result_authority_ref=_ref(
                    'synthetic_execution_result',
                    'phantom-result',
                    '0',
                ),
                execution_provenance_ref=_ref(
                    'synthetic_execution_provenance',
                    'phantom-provenance',
                    '1',
                ),
            )

    with BoundedR140Executor(
        execution_repository=fx.execution,
        runtime_repository=fx.runtime,
        worker_port=PhantomRefWorker(),
        max_workers=1,
    ) as executor:
        summary = executor.run_schedule(schedule)

    assert len(summary.failed) == 1
    attempt = summary.failed[0]
    assert attempt.state == 'FAILED'
    assert attempt.execution_result is None
    assert attempt.telemetry.exit_condition == 'result_publication_failure'
    assert (
        'exact external authority does not exist'
        in attempt.telemetry.failure_reason
    )
    assert _row_counts(fx.scene.path) == _failed_publication_counts()
    assert fx.execution.reusable_cache(task) is None
    persisted = fx.runtime.list_attempts(task.task_id)
    assert [item.state for item in persisted] == ['FAILED']
    assert persisted[0].execution_result is None


def test_tampered_worker_authority_publishes_no_success_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fx = Fixture(tmp_path, candidate_count=1)
    task, _ = fx.add_task(0)
    schedule = fx.schedule(cpu_capacity=1)

    original_resolve = fx.execution.external_authority_resolver

    def tampered_resolve(ref: MultiFidelityAuthorityRef):
        resolved = original_resolve(ref)
        if (
            resolved is not None
            and ref.authority_kind == 'synthetic_execution_result'
        ):
            return resolved.model_copy(update={'authority_version': 'forged'})
        return resolved

    monkeypatch.setattr(
        fx.execution,
        'external_authority_resolver',
        tampered_resolve,
    )

    with BoundedR140Executor(
        execution_repository=fx.execution,
        runtime_repository=fx.runtime,
        worker_port=DeterministicSyntheticWorker(register_ref=fx.register),
        max_workers=1,
    ) as executor:
        summary = executor.run_schedule(schedule)

    assert len(summary.failed) == 1
    attempt = summary.failed[0]
    assert attempt.telemetry.exit_condition == 'result_publication_failure'
    assert (
        'exact external authority mismatch'
        in attempt.telemetry.failure_reason
    )
    assert _row_counts(fx.scene.path) == _failed_publication_counts()
    assert fx.execution.reusable_cache(task) is None


@pytest.mark.parametrize(
    'fault_target',
    (
        'validate_cache_entry',
        'save_result_in_transaction',
        'save_cache_entry_in_transaction',
        'save_attempt_in_transaction',
    ),
)
def test_success_publish_rolls_back_everything_on_fault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_target: str,
) -> None:
    """Fault at any validation/write boundary leaves no partial success rows."""
    fx = Fixture(tmp_path, candidate_count=1)
    task, _ = fx.add_task(0)
    schedule = fx.schedule(cpu_capacity=1)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError('injected publication fault')

    if fault_target == 'validate_cache_entry':
        monkeypatch.setattr(fx.execution, '_validate_cache_entry', boom)
    elif fault_target == 'save_result_in_transaction':
        monkeypatch.setattr(fx.runtime, '_save_result_in_transaction', boom)
    elif fault_target == 'save_cache_entry_in_transaction':
        monkeypatch.setattr(
            fx.execution,
            '_save_cache_entry_in_transaction',
            boom,
        )
    else:
        original = fx.runtime._save_attempt_in_transaction
        fired = False

        def fail_once(connection, record):
            nonlocal fired
            if not fired:
                fired = True
                raise sqlite3.OperationalError('injected publication fault')
            return original(connection, record)

        # The executor retries the attempt write through save_attempt after the
        # rolled-back publication; only the atomic transaction call fails.
        monkeypatch.setattr(
            fx.runtime,
            '_save_attempt_in_transaction',
            fail_once,
        )

    with BoundedR140Executor(
        execution_repository=fx.execution,
        runtime_repository=fx.runtime,
        worker_port=DeterministicSyntheticWorker(register_ref=fx.register),
        max_workers=1,
    ) as executor:
        summary = executor.run_schedule(schedule)

    assert len(summary.failed) == 1
    attempt = summary.failed[0]
    assert attempt.state == 'FAILED'
    assert attempt.execution_result is None
    assert attempt.telemetry.exit_condition == 'result_publication_failure'
    assert 'injected publication fault' in attempt.telemetry.failure_reason
    assert _row_counts(fx.scene.path) == _failed_publication_counts()
    assert fx.execution.reusable_cache(task) is None
    assert [
        item.state for item in fx.runtime.list_attempts(task.task_id)
    ] == ['FAILED']


def test_reopen_rejects_result_when_external_authority_is_dropped(
    tmp_path: Path,
) -> None:
    fx = Fixture(tmp_path, candidate_count=1)
    task, _ = fx.add_task(0)
    schedule = fx.schedule(cpu_capacity=1)

    with BoundedR140Executor(
        execution_repository=fx.execution,
        runtime_repository=fx.runtime,
        worker_port=DeterministicSyntheticWorker(register_ref=fx.register),
        max_workers=1,
    ) as executor:
        summary = executor.run_schedule(schedule)

    assert len(summary.succeeded) == 1
    result = summary.succeeded[0].execution_result
    assert result is not None
    assert fx.runtime.get_result(result.execution_result_id) == result
    assert (
        fx.runtime.list_attempts(task.task_id)[0].execution_result == result
    )

    fx.authorities.pop(result.result_authority_ref.key())
    with pytest.raises(
        ValueError,
        match='execution result exact external authority does not exist',
    ):
        fx.runtime.get_result(result.execution_result_id)
    with pytest.raises(
        ValueError,
        match='execution result exact external authority does not exist',
    ):
        fx.runtime.list_attempts(task.task_id)

    fx.register(result.result_authority_ref)
    fx.authorities.pop(result.execution_provenance_ref.key())
    with pytest.raises(
        ValueError,
        match='execution provenance exact external authority does not exist',
    ):
        fx.runtime.get_result(result.execution_result_id)
    with pytest.raises(
        ValueError,
        match='execution provenance exact external authority does not exist',
    ):
        fx.runtime.list_attempts(task.task_id)


def test_commit_success_requires_coherent_succeeded_triple(
    tmp_path: Path,
) -> None:
    fx = Fixture(tmp_path, candidate_count=2)
    task_a, _ = fx.add_task(0)
    task_b, _ = fx.add_task(1)
    schedule = fx.schedule(cpu_capacity=2)

    worker = DeterministicSyntheticWorker(
        fail_task_ids=(task_b.task_id,),
        register_ref=fx.register,
    )
    with BoundedR140Executor(
        execution_repository=fx.execution,
        runtime_repository=fx.runtime,
        worker_port=worker,
        max_workers=2,
    ) as executor:
        summary = executor.run_schedule(schedule)

    success = next(
        item for item in summary.attempts if item.state == 'SUCCEEDED'
    )
    failed = next(item for item in summary.attempts if item.state == 'FAILED')
    result = success.execution_result
    assert result is not None
    real_cache = fx.execution.reusable_cache(task_a)
    assert real_cache is not None

    before = _row_counts(fx.scene.path)

    # A non-SUCCEEDED attempt cannot publish success artifacts.
    with pytest.raises(
        ValueError,
        match='R140 success publication requires a SUCCEEDED attempt',
    ):
        fx.runtime.commit_success(
            result=result,
            cache_entry=real_cache,
            attempt=failed,
        )

    # The cache entry must match the committed result exactly.
    mismatched_cache = build_multifidelity_execution_cache_entry(
        task=task_b,
        result_authority_ref=result.result_authority_ref,
        execution_provenance_ref=result.execution_provenance_ref,
        completed_at_utc='2026-09-20T00:00:00+00:00',
    )
    with pytest.raises(
        ValueError,
        match='R140 success cache entry does not match the committed result',
    ):
        fx.runtime.commit_success(
            result=result,
            cache_entry=mismatched_cache,
            attempt=success,
        )

    # The attempt must embed exactly the co-committed result.
    other_output = ExecutionWorkerOutput(
        result_authority_ref=_ref(
            'synthetic_execution_result',
            'other-result',
            'c',
        ),
        execution_provenance_ref=_ref(
            'synthetic_execution_provenance',
            'other-provenance',
            'd',
        ),
    )
    fx.register(other_output.result_authority_ref)
    fx.register(other_output.execution_provenance_ref)
    other_result = build_execution_task_result(
        task=task_a,
        output=other_output,
    )
    other_cache = build_multifidelity_execution_cache_entry(
        task=task_a,
        result_authority_ref=other_result.result_authority_ref,
        execution_provenance_ref=other_result.execution_provenance_ref,
        completed_at_utc='2026-09-20T00:00:00+00:00',
    )
    with pytest.raises(
        ValueError,
        match='does not match the co-committed result',
    ):
        fx.runtime.commit_success(
            result=other_result,
            cache_entry=other_cache,
            attempt=success,
        )
    assert _row_counts(fx.scene.path) == before

    # Re-committing the identical triple is idempotent.
    again = fx.runtime.commit_success(
        result=result,
        cache_entry=real_cache,
        attempt=success,
    )
    assert again == success
    assert _row_counts(fx.scene.path) == before


def test_save_result_resolves_external_authorities_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fx = Fixture(tmp_path, candidate_count=1)
    task, _ = fx.add_task(0)
    output = ExecutionWorkerOutput(
        result_authority_ref=_ref(
            'synthetic_execution_result',
            'untracked-result',
            '0',
        ),
        execution_provenance_ref=_ref(
            'synthetic_execution_provenance',
            'untracked-provenance',
            '1',
        ),
    )
    result = build_execution_task_result(task=task, output=output)

    with pytest.raises(
        ValueError,
        match='execution result exact external authority does not exist',
    ):
        fx.runtime.save_result(result)

    fx.register(output.result_authority_ref)
    fx.register(output.execution_provenance_ref)
    original_resolve = fx.execution.external_authority_resolver

    def tampered_resolve(ref: MultiFidelityAuthorityRef):
        resolved = original_resolve(ref)
        if (
            resolved is not None
            and ref.authority_kind == 'synthetic_execution_result'
        ):
            return resolved.model_copy(update={'authority_version': 'forged'})
        return resolved

    monkeypatch.setattr(
        fx.execution,
        'external_authority_resolver',
        tampered_resolve,
    )
    with pytest.raises(
        ValueError,
        match='execution result exact external authority mismatch',
    ):
        fx.runtime.save_result(result)

    assert _row_counts(fx.scene.path)['cad_r140_execution_results'] == 0
    assert fx.runtime.get_result(result.execution_result_id) is None
