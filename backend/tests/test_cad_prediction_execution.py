from __future__ import annotations

from pathlib import Path

from htdt.cad_multifidelity import (
    CadMultiFidelityRepository,
    MultiFidelityAuthorityRef,
    MultiFidelityStageDefinition,
    build_multifidelity_plan,
)
from htdt.cad_multifidelity_execution import (
    CadMultiFidelityExecutionRepository,
    ExecutionCapacityAuthority,
    ExecutionResourceVector,
    build_multifidelity_execution_schedule,
    build_multifidelity_execution_task,
)
from htdt.cad_prediction_execution import (
    PredictionExecutionController,
    PredictionExecutionScope,
    ResourceQuantityView,
    build_prediction_execution_preflight,
    confirm_execution,
)
from htdt.cad_r140_executor import (
    BoundedR140Executor,
    CadR140ExecutorRepository,
    CpuResourceEstimateRequest,
    DeclaredCpuResourceEstimator,
    ExecutionWorkerOutput,
    ResourceQuantity,
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


def _plan():
    candidates = (
        _ref('system_variant', 'variant-0', 'a'),
        _ref('system_variant', 'variant-1', 'b'),
    )
    stages = (
        MultiFidelityStageDefinition(
            stage_id='cpu',
            order=0,
            name='CPU baseline',
            policy='hard_gate',
            fidelity_label='fixture',
            evaluator_authority=_ref(
                'objective_evaluator', 'cpu-evaluator', 'd', evaluator=True
            ),
        ),
        MultiFidelityStageDefinition(
            stage_id='final',
            order=1,
            name='Final common fidelity',
            policy='final_common_fidelity',
            fidelity_label='fixture-final',
            evaluator_authority=_ref(
                'objective_evaluator', 'final-evaluator', '8', evaluator=True
            ),
        ),
    )
    return build_multifidelity_plan(
        domain='o90_robustness',
        name='preflight fixture',
        baseline_authority=_ref('scene_revision', 'baseline', 'e'),
        candidates=candidates,
        stages=stages,
    )


class Fixture:
    def __init__(self, tmp_path: Path) -> None:
        self.scene = SceneRepository(tmp_path / 'cad.sqlite3')
        self.authorities = {}

        def resolve(ref: MultiFidelityAuthorityRef, context=None):
            return self.authorities.get(ref.key())

        self.multifidelity = CadMultiFidelityRepository(
            self.scene,
            authority_resolver=resolve,
            stage_evidence_resolver=resolve,
        )
        self.plan = _plan()
        for ref in [
            self.plan.baseline_authority,
            *self.plan.candidates,
            *(stage.evaluator_authority for stage in self.plan.stages),
        ]:
            self.authorities[ref.key()] = ref
        self.multifidelity.save_plan(self.plan)
        self.execution = CadMultiFidelityExecutionRepository(
            self.scene,
            multifidelity_repository=self.multifidelity,
            external_authority_resolver=resolve,
        )
        self.runtime = CadR140ExecutorRepository(
            self.scene,
            execution_repository=self.execution,
        )
        self.estimator = DeclaredCpuResourceEstimator()

    def register(self, ref: MultiFidelityAuthorityRef) -> None:
        self.authorities[ref.key()] = ref

    def add_task(
        self,
        *,
        logical_cpu: int = 1,
        inner_threads: int = 1,
        memory_state: str = 'known',
    ):
        backend = _ref('execution_backend', 'cpu-thread-worker', 'f')
        config = _ref('execution_configuration', 'cpu-config-1', '1')
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
                peak_memory_bytes=(
                    ResourceQuantity.known(1_000_000, 'bytes')
                    if memory_state == 'known'
                    else ResourceQuantity.unknown('bytes')
                ),
                scratch_bytes=ResourceQuantity.known(100_000, 'bytes'),
                estimate_method='fixture-declared',
                estimate_method_version='1',
                confidence='HIGH',
            )
        )
        for ref in (backend, config, estimate.authority_ref()):
            self.register(ref)
        self.runtime.save_resource_estimate(estimate)
        task = build_multifidelity_execution_task(
            plan=self.plan,
            stage_id='cpu',
            candidate=self.plan.candidates[0],
            execution_backend_ref=backend,
            execution_configuration_ref=config,
            resource_estimate_ref=estimate.authority_ref(),
            resource_request=estimate.admission_resource_vector(),
        )
        self.execution.save_task(task)
        return task, estimate

    def scope(self, scene_content_hash: str) -> PredictionExecutionScope:
        return PredictionExecutionScope(
            operation_label='Room prediction',
            provider_label='R130 deterministic wave solver',
            requested_observables=('frequency_response_magnitude',),
            fidelity_label='fixture',
            scene_content_hash=scene_content_hash,
        )


SCENE_HASH = 'c' * 64


def test_preflight_admits_known_estimate(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    task, estimate = fx.add_task()
    preflight = build_prediction_execution_preflight(
        task=task,
        estimate=estimate,
        scope=fx.scope(SCENE_HASH),
        cache_entry=None,
        prior_attempts=(),
        current_scene_content_hash=SCENE_HASH,
    )
    assert preflight.admission_state == 'ADMITTED'
    assert preflight.cache_state == 'NO_EXACT_RESULT'
    assert preflight.currency == 'CURRENT'
    assert preflight.requires_explicit_confirmation is False
    assert preflight.preflight_id.startswith('prediction-preflight:')
    assert not preflight.blockers


def test_preflight_defers_when_estimate_unknown(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    task, _ = fx.add_task()
    backend = _ref('execution_backend', 'cpu-thread-worker', 'f')
    config = _ref('execution_configuration', 'cpu-config-1', '1')
    estimate = fx.estimator.estimate(
        CpuResourceEstimateRequest(
            execution_backend_ref=backend,
            execution_configuration_ref=config,
            logical_cpu_demand=ResourceQuantity.known(1, 'logical_cpus'),
            physical_core_demand=ResourceQuantity.unknown('cores'),
            inner_solver_threads=ResourceQuantity.known(1, 'threads'),
            peak_memory_bytes=ResourceQuantity.unknown('bytes'),
            scratch_bytes=ResourceQuantity.known(100_000, 'bytes'),
            estimate_method='fixture-declared',
            estimate_method_version='1',
            confidence='LOW',
        )
    )
    # task pins its own estimate; rebuild task against the unknown-memory one.
    fx.register(estimate.authority_ref())
    fx.runtime.save_resource_estimate(estimate)
    task = build_multifidelity_execution_task(
        plan=fx.plan,
        stage_id='cpu',
        candidate=fx.plan.candidates[0],
        execution_backend_ref=backend,
        execution_configuration_ref=config,
        resource_estimate_ref=estimate.authority_ref(),
        resource_request=ExecutionResourceVector(
            cpu_threads=1, gpu_slots=0, memory_bytes=1, scratch_bytes=1
        ),
    )
    preflight = build_prediction_execution_preflight(
        task=task,
        estimate=estimate,
        scope=fx.scope(SCENE_HASH),
        cache_entry=None,
        prior_attempts=(),
        current_scene_content_hash=SCENE_HASH,
    )
    assert preflight.admission_state == 'DEFER'
    assert preflight.requires_explicit_confirmation is True
    assert any('DEFER' in blocker for blocker in preflight.blockers)


def test_preflight_rejects_when_admission_unsafe(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    fx.add_task()
    backend = _ref('execution_backend', 'cpu-thread-worker', 'f')
    config = _ref('execution_configuration', 'cpu-config-1', '1')
    estimate = fx.estimator.estimate(
        CpuResourceEstimateRequest(
            execution_backend_ref=backend,
            execution_configuration_ref=config,
            logical_cpu_demand=ResourceQuantity.known(1, 'logical_cpus'),
            physical_core_demand=ResourceQuantity.unknown('cores'),
            inner_solver_threads=ResourceQuantity.known(8, 'threads'),
            peak_memory_bytes=ResourceQuantity.known(1_000, 'bytes'),
            scratch_bytes=ResourceQuantity.known(0, 'bytes'),
            estimate_method='fixture-declared',
            estimate_method_version='1',
            confidence='HIGH',
        )
    )
    fx.register(estimate.authority_ref())
    fx.runtime.save_resource_estimate(estimate)
    task = build_multifidelity_execution_task(
        plan=fx.plan,
        stage_id='cpu',
        candidate=fx.plan.candidates[0],
        execution_backend_ref=backend,
        execution_configuration_ref=config,
        resource_estimate_ref=estimate.authority_ref(),
        resource_request=ExecutionResourceVector(
            cpu_threads=1, gpu_slots=0, memory_bytes=1, scratch_bytes=1
        ),
    )
    preflight = build_prediction_execution_preflight(
        task=task,
        estimate=estimate,
        scope=fx.scope(SCENE_HASH),
        cache_entry=None,
        prior_attempts=(),
        current_scene_content_hash=SCENE_HASH,
    )
    assert preflight.admission_state == 'REJECT'
    assert preflight.blockers


def test_preflight_stale_scene_forces_full_rerun(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    task, estimate = fx.add_task()
    preflight = build_prediction_execution_preflight(
        task=task,
        estimate=estimate,
        scope=fx.scope(SCENE_HASH),
        cache_entry=None,
        prior_attempts=(),
        current_scene_content_hash='9' * 64,
    )
    assert preflight.currency == 'STALE_SCENE'
    assert preflight.admission_state == 'REJECT'
    assert 'full rerun' in preflight.blockers[0]


def test_progress_cancellation_and_attempt_history(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    task, _ = fx.add_task()
    capacity_ref = _ref('hardware_capacity', 'cpu-capacity', '9')
    fx.register(capacity_ref)
    schedule = build_multifidelity_execution_schedule(
        tasks=(task,),
        capacity_authority=ExecutionCapacityAuthority(
            capacity_authority_ref=capacity_ref,
            capacity=task.resource_request.model_copy(
                update={'memory_bytes': 10_000_000, 'scratch_bytes': 10_000_000}
            ),
        ),
    )
    fx.execution.save_schedule(schedule)
    # worker output authorities must be resolvable for result publication
    fx.register(_ref('solver_result', 'result-1', '5'))
    fx.register(_ref('provenance', 'prov-1', '6'))

    class SlowWorker:
        def __call__(self, task, context):
            context.raise_if_cancelled()
            import time

            time.sleep(0.2)
            context.raise_if_cancelled()
            return ExecutionWorkerOutput(
                result_authority_ref=_ref('solver_result', 'result-1', '5'),
                execution_provenance_ref=_ref('provenance', 'prov-1', '6'),
            )

    with BoundedR140Executor(
        execution_repository=fx.execution,
        runtime_repository=fx.runtime,
        worker_port=SlowWorker(),
        max_workers=1,
    ) as executor:
        controller = PredictionExecutionController(executor)
        view = controller.progress_view(task.task_id)
        assert view.phase == 'QUEUED'

        accepted = controller.request_cancellation(task.task_id)
        assert accepted is False  # not yet registered by the executor

        summary = controller.run_schedule(schedule)
        assert len(summary.attempts) == 1
        assert summary.attempts[0].state in ('SUCCEEDED', 'CANCELLED')

    history = controller.attempt_history(task.task_id)
    assert len(history) == 1
    assert history[0].state in ('SUCCEEDED', 'FAILED', 'CANCELLED')
    view = controller.progress_view(
        task.task_id, latest_attempt=summary.attempts[0]
    )
    assert view.phase in ('SUCCEEDED', 'CANCELLED')


def test_confirm_execution_never_admits_blocked_preflight(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    task, estimate = fx.add_task()
    scope = fx.scope(SCENE_HASH)
    admitted_preflight = build_prediction_execution_preflight(
        task=task,
        estimate=estimate,
        scope=scope,
        cache_entry=None,
        prior_attempts=(),
        current_scene_content_hash=SCENE_HASH,
    )
    decision = confirm_execution(
        admitted_preflight, task=task, estimate=estimate
    )
    assert decision.admitted is True
    assert decision.task is task

    stale_preflight = build_prediction_execution_preflight(
        task=task,
        estimate=estimate,
        scope=scope,
        cache_entry=None,
        prior_attempts=(),
        current_scene_content_hash='9' * 64,
    )
    decision = confirm_execution(
        stale_preflight, task=task, estimate=estimate
    )
    assert decision.admitted is False
    assert decision.task is None


def test_resource_quantity_view_preserves_unknown() -> None:
    view = ResourceQuantityView.from_quantity(
        'memory', ResourceQuantity.unknown('bytes')
    )
    assert view.state == 'UNKNOWN'
    assert view.value is None
    known = ResourceQuantityView.from_quantity(
        'cpu', ResourceQuantity.known(4, 'logical_cpus')
    )
    assert known.state == 'KNOWN'
    assert known.value == 4
