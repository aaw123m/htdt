from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys
import traceback

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.run_r130a_candidate_wave_execution import (
    FIXTURE_ID,
    PFFDTD_SHA,
    _fixture,
)

from htdt.cad_multifidelity import (
    CadMultiFidelityRepository,
    MultiFidelityAuthorityRef,
    MultiFidelityStageDefinition,
    build_multifidelity_plan,
)
from htdt.cad_multifidelity_execution import (
    CadMultiFidelityExecutionRepository,
    ExecutionCapacityAuthority,
    build_multifidelity_execution_schedule,
    build_multifidelity_execution_task,
)
from htdt.cad_pffdtd_resource_estimator import (
    PFFDTD_RESOURCE_ESTIMATOR_ID,
    PFFDTD_RESOURCE_ESTIMATOR_VERSION,
    PffdtdCandidateResourceEstimator,
    PffdtdR140Worker,
    pffdtd_candidate_input_ref,
)
from htdt.cad_r140_executor import (
    BoundedR140Executor,
    CadR140ExecutorRepository,
)
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


def _hash(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


def _ref(
    kind: str,
    authority_id: str,
    payload: object,
    *,
    evaluator: bool = False,
) -> MultiFidelityAuthorityRef:
    kwargs = {}
    if evaluator:
        kwargs = {
            'evaluator_id': f'evaluator:{authority_id}',
            'evaluator_version': '1',
            'model_id': f'model:{authority_id}',
            'model_version': '1',
            'fidelity': 'r130a-candidate',
        }
    return MultiFidelityAuthorityRef(
        authority_kind=kind,
        authority_id=authority_id,
        authority_version='1',
        semantic_sha256=_hash(payload),
        **kwargs,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Run R140 with the real bounded PFFDTD candidate workload'
    )
    parser.add_argument('--upstream-root', required=True, type=Path)
    parser.add_argument('--work-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.work_root.exists():
        shutil.rmtree(args.work_root)
    args.work_root.mkdir(parents=True)

    payload: dict[str, object] = {
        'schema_version': 'htdt.r140.pffdtd-resource-executor-evidence-1',
        'fixture_id': FIXTURE_ID,
        'candidate_source_commit_sha': PFFDTD_SHA,
        'estimator_id': PFFDTD_RESOURCE_ESTIMATOR_ID,
        'estimator_version': PFFDTD_RESOURCE_ESTIMATOR_VERSION,
        'production_solver_selected': False,
        'gpu_validation_state': 'NOT_VALIDATED',
        'gpu_numerical_equivalence_validated': False,
        'owned_room_evidence': False,
        'rdc_calls': 0,
    }
    status = 'PASS'

    try:
        fixture = _fixture(
            args.work_root / 'candidate-fixture',
            args.upstream_root,
        )
        candidate_input, model = fixture['executor'].compile_input(
            dispatch_binding_id=fixture['dispatch'].binding_id,
            configuration=fixture['configuration'],
        )
        sound_speed = fixture['snapshot'].environment.sound_speed_m_s
        assert sound_speed is not None
        estimation = PffdtdCandidateResourceEstimator().estimate(
            authority=candidate_input,
            model=model,
            configuration=fixture['configuration'],
            sound_speed_m_s=sound_speed,
        )
        estimate = estimation.execution_resource_estimate
        vector = estimate.admission_resource_vector()

        workload_exact_ref = ExactExternalAuthorityRef(
            authority_id=estimation.workload.workload_estimate_id,
            authority_version=estimation.workload.authority_version,
            semantic_hash_sha256=estimation.workload.semantic_sha256,
        )
        fixture['store'].put_exact_json(
            workload_exact_ref,
            estimation.workload.semantic_payload(),
        )

        candidate_ref = pffdtd_candidate_input_ref(candidate_input)
        evaluator_ref = _ref(
            'objective_evaluator',
            'r140-pffdtd-execution-evaluator',
            {'fixture': FIXTURE_ID, 'version': 1},
            evaluator=True,
        )
        other_candidate = _ref(
            'candidate_wave_execution_input',
            'r140-unused-plan-placeholder',
            {'fixture': FIXTURE_ID, 'placeholder': True},
        )
        plan = build_multifidelity_plan(
            domain='o90_robustness',
            name='R140 exact PFFDTD resource/executor evidence',
            baseline_authority=_ref(
                'scene_revision',
                'r140-pffdtd-baseline',
                {'fixture': FIXTURE_ID, 'baseline': True},
            ),
            candidates=(candidate_ref, other_candidate),
            stages=(
                MultiFidelityStageDefinition(
                    stage_id='execute',
                    order=0,
                    name='Execute exact PFFDTD candidate',
                    policy='shortlist_only',
                    fidelity_label='r130a-candidate',
                    evaluator_authority=evaluator_ref,
                ),
                MultiFidelityStageDefinition(
                    stage_id='final',
                    order=1,
                    name='Non-production final placeholder',
                    policy='final_common_fidelity',
                    fidelity_label='r130a-candidate',
                    evaluator_authority=evaluator_ref,
                ),
            ),
        )
        multifidelity_repository = CadMultiFidelityRepository(
            fixture['scene_repository']
        )
        multifidelity_repository.save_plan(plan)

        registry: dict[
            tuple[str, str, str | None, str],
            MultiFidelityAuthorityRef,
        ] = {}

        def register(ref: MultiFidelityAuthorityRef) -> None:
            registry[ref.key()] = ref

        register(evaluator_ref)
        register(estimate.execution_backend_ref)
        register(estimate.execution_configuration_ref)
        register(estimate.authority_ref())

        def resolve(ref: MultiFidelityAuthorityRef):
            exact = registry.get(ref.key())
            if exact is not None:
                return exact
            if ref.authority_kind == 'acoustic_solver_result':
                result = fixture['result_repository'].get(ref.authority_id)
                if (
                    result is not None
                    and result.semantic_sha256 == ref.semantic_sha256
                    and result.authority_version == ref.authority_version
                ):
                    return ref
                return None
            if ref.authority_kind == 'solver_execution_provenance':
                external = ExactExternalAuthorityRef(
                    authority_id=ref.authority_id,
                    authority_version=ref.authority_version,
                    semantic_hash_sha256=ref.semantic_sha256,
                )
                try:
                    fixture['store'].read_payload(external)
                except ValueError:
                    return None
                return ref
            return None

        execution_repository = CadMultiFidelityExecutionRepository(
            fixture['scene_repository'],
            multifidelity_repository=multifidelity_repository,
            external_authority_resolver=resolve,
        )
        runtime_repository = CadR140ExecutorRepository(
            fixture['scene_repository'],
            execution_repository=execution_repository,
        )
        runtime_repository.save_resource_estimate(estimate)

        task = build_multifidelity_execution_task(
            plan=plan,
            stage_id='execute',
            candidate=candidate_ref,
            execution_backend_ref=estimate.execution_backend_ref,
            execution_configuration_ref=estimate.execution_configuration_ref,
            resource_estimate_ref=estimate.authority_ref(),
            resource_request=vector,
        )
        execution_repository.save_task(task)

        capacity_ref = _ref(
            'hardware_capacity',
            'github-actions-r140-pffdtd-capacity',
            {
                'fixture': FIXTURE_ID,
                'logical_cpu_available': os.cpu_count() or 1,
                'task_resource_vector': vector.model_dump(mode='json'),
                'scope': 'dedicated one-task evidence job',
            },
        )
        register(capacity_ref)
        if (os.cpu_count() or 1) < vector.cpu_threads:
            raise RuntimeError(
                'GitHub Actions runner does not satisfy exact logical CPU '
                f'reservation: available={os.cpu_count() or 1}, '
                f'required={vector.cpu_threads}'
            )

        schedule = build_multifidelity_execution_schedule(
            tasks=(task,),
            capacity_authority=ExecutionCapacityAuthority(
                capacity_authority_ref=capacity_ref,
                capacity=vector,
            ),
        )
        execution_repository.save_schedule(schedule)

        worker = PffdtdR140Worker(
            candidate_executor=fixture['executor'],
            dispatch_binding_id=fixture['dispatch'].binding_id,
            configuration=fixture['configuration'],
            expected_input=candidate_input,
        )
        with BoundedR140Executor(
            execution_repository=execution_repository,
            runtime_repository=runtime_repository,
            worker_port=worker,
            max_workers=1,
        ) as executor:
            first = executor.run_schedule(schedule)

        assert len(first.succeeded) == 1
        assert not first.failed
        assert not first.cancelled
        attempt = first.succeeded[0]
        allocation = attempt.telemetry.worker_allocation
        assert allocation is not None
        assert allocation.logical_cpu_reserved == vector.cpu_threads
        assert allocation.inner_solver_threads == (
            estimation.workload.inner_solver_threads
        )
        assert allocation.memory_reserved_bytes == vector.memory_bytes
        assert allocation.scratch_reserved_bytes == vector.scratch_bytes
        assert allocation.gpu_slots_reserved == 0
        assert attempt.telemetry.scratch_usage.state == 'KNOWN'
        assert attempt.telemetry.scratch_usage.value is not None
        assert attempt.telemetry.scratch_usage.value <= vector.scratch_bytes

        exact_result_id = (
            attempt.execution_result.result_authority_ref.authority_id
        )
        solver_result = fixture['result_repository'].get(exact_result_id)
        assert solver_result is not None
        provenance = fixture['store'].read_payload(
            solver_result.execution_provenance_ref
        )
        assert provenance['grid_shape'] == list(estimation.workload.grid_shape)
        assert (
            provenance['time_step_count']
            == estimation.workload.time_step_count
        )
        assert provenance['production_solver_selected'] is False

        with BoundedR140Executor(
            execution_repository=execution_repository,
            runtime_repository=runtime_repository,
            worker_port=worker,
            max_workers=1,
        ) as executor:
            resumed = executor.run_schedule(schedule)
        assert not resumed.attempts
        assert len(resumed.reused_cache_entries) == 1
        assert resumed.reused_cache_entries[0].task_id == task.task_id

        payload.update(
            {
                'status': status,
                'candidate_execution_input_id': (
                    candidate_input.execution_input_id
                ),
                'candidate_execution_input_sha256': (
                    candidate_input.semantic_sha256
                ),
                'workload_estimate': estimation.workload.model_dump(mode='json'),
                'execution_resource_estimate': estimate.model_dump(mode='json'),
                'resource_vector': vector.model_dump(mode='json'),
                'schedule_id': schedule.schedule_id,
                'task_id': task.task_id,
                'executor_result': (
                    attempt.execution_result.model_dump(mode='json')
                ),
                'telemetry': attempt.telemetry.model_dump(mode='json'),
                'cache_resume': {
                    'first_run_attempts': len(first.attempts),
                    'second_run_attempts': len(resumed.attempts),
                    'second_run_reused_cache_entries': (
                        len(resumed.reused_cache_entries)
                    ),
                },
                'actual_solver_grid_shape': provenance['grid_shape'],
                'actual_solver_time_step_count': provenance['time_step_count'],
                'actual_solver_runtime_identity': provenance['runtime_identity'],
            }
        )
    except Exception as exc:
        status = 'FAIL'
        payload.update(
            {
                'status': status,
                'error': f'{type(exc).__name__}: {exc}',
                'traceback': traceback.format_exc(),
            }
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if status == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
