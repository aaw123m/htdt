from __future__ import annotations

from pathlib import Path

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
    ExecutionResourceVector,
    build_multifidelity_execution_cache_entry,
    build_multifidelity_execution_schedule,
    build_multifidelity_execution_task,
)
from htdt.cad_repository import SceneRepository


NOW = '2026-09-20T00:00:00+00:00'


def _ref(
    kind: str,
    authority_id: str,
    char: str,
    *,
    evaluator: bool = False,
    fidelity: str | None = None,
) -> MultiFidelityAuthorityRef:
    kwargs = {}
    if evaluator:
        kwargs.update(
            evaluator_id=f'evaluator:{authority_id}',
            evaluator_version='1',
            model_id=f'model:{authority_id}',
            model_version='1',
            fidelity=fidelity or 'fixture-fidelity',
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
        _ref('system_variant', 'variant-a', 'a'),
        _ref('system_variant', 'variant-b', 'b'),
    )
    stages = (
        MultiFidelityStageDefinition(
            stage_id='coarse',
            order=0,
            name='Coarse screening',
            policy='hard_gate',
            fidelity_label='coarse',
            evaluator_authority=_ref(
                'objective_evaluator',
                'coarse-evaluator',
                'c',
                evaluator=True,
                fidelity='coarse',
            ),
        ),
        MultiFidelityStageDefinition(
            stage_id='final',
            order=1,
            name='Final common fidelity',
            policy='final_common_fidelity',
            fidelity_label='final',
            evaluator_authority=_ref(
                'objective_evaluator',
                'final-evaluator',
                'd',
                evaluator=True,
                fidelity='final',
            ),
        ),
    )
    return build_multifidelity_plan(
        domain='o90_robustness',
        name='R140 execution fixture',
        baseline_authority=_ref('scene_revision', 'baseline', 'e'),
        candidates=candidates,
        stages=stages,
    )


def _task(plan, candidate, *, config_char: str, estimate_char: str):
    return build_multifidelity_execution_task(
        plan=plan,
        stage_id='coarse',
        candidate=candidate,
        execution_backend_ref=_ref(
            'execution_backend',
            'cpu-gpu-runner',
            'f',
        ),
        execution_configuration_ref=_ref(
            'execution_configuration',
            f'config-{config_char}',
            config_char,
        ),
        resource_estimate_ref=_ref(
            'resource_estimate',
            f'estimate-{estimate_char}',
            estimate_char,
        ),
        device_refs=(
            _ref('hardware_device', 'gpu-0', '9'),
        ),
        resource_request=ExecutionResourceVector(
            cpu_threads=2,
            gpu_slots=1,
            memory_bytes=4_000_000,
            scratch_bytes=1_000_000,
        ),
    )


def _registry(*refs: MultiFidelityAuthorityRef):
    values = {ref.key(): ref for ref in refs}

    def resolve(ref: MultiFidelityAuthorityRef):
        return values.get(ref.key())

    return values, resolve


def _task_refs(task):
    return (
        task.evaluator_authority,
        task.execution_backend_ref,
        task.execution_configuration_ref,
        task.resource_estimate_ref,
        *task.device_refs,
    )


def test_r140_task_identity_changes_with_exact_execution_configuration() -> None:
    plan = _plan()
    first = _task(
        plan,
        plan.candidates[0],
        config_char='1',
        estimate_char='2',
    )
    repeated = _task(
        plan,
        plan.candidates[0],
        config_char='1',
        estimate_char='2',
    )
    changed = _task(
        plan,
        plan.candidates[0],
        config_char='3',
        estimate_char='2',
    )

    assert repeated == first
    assert changed.task_id != first.task_id
    assert changed.execution_input_sha256 != first.execution_input_sha256


def test_r140_schedule_batches_without_declared_gpu_oversubscription() -> None:
    plan = _plan()
    task_a = _task(
        plan,
        plan.candidates[0],
        config_char='1',
        estimate_char='2',
    )
    task_b = _task(
        plan,
        plan.candidates[1],
        config_char='1',
        estimate_char='4',
    )
    capacity = ExecutionCapacityAuthority(
        capacity_authority_ref=_ref(
            'hardware_capacity',
            'fixture-capacity',
            '5',
        ),
        capacity=ExecutionResourceVector(
            cpu_threads=4,
            gpu_slots=1,
            memory_bytes=8_000_000,
            scratch_bytes=2_000_000,
        ),
    )

    schedule = build_multifidelity_execution_schedule(
        tasks=(task_b, task_a),
        capacity_authority=capacity,
    )

    assert len(schedule.batches) == 2
    assert all(batch.aggregate_resources.gpu_slots == 1 for batch in schedule.batches)
    assert tuple(
        ref.task_id for ref in schedule.task_refs
    ) == tuple(
        sorted(
            (task_a, task_b),
            key=lambda item: (
                item.stage_order,
                item.candidate.authority_id,
                item.task_id,
            ),
        )[index].task_id
        for index in range(2)
    )


def test_r140_schedule_rejects_task_larger_than_capacity() -> None:
    plan = _plan()
    task = _task(
        plan,
        plan.candidates[0],
        config_char='1',
        estimate_char='2',
    )
    capacity = ExecutionCapacityAuthority(
        capacity_authority_ref=_ref(
            'hardware_capacity',
            'small-capacity',
            '5',
        ),
        capacity=ExecutionResourceVector(
            cpu_threads=1,
            gpu_slots=0,
            memory_bytes=2_000_000,
            scratch_bytes=500_000,
        ),
    )

    with pytest.raises(ValueError, match='exceeds declared capacity'):
        build_multifidelity_execution_schedule(
            tasks=(task,),
            capacity_authority=capacity,
        )


def test_r140_cache_resume_reopens_exact_result_and_fails_when_stale(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    multifidelity_repository = CadMultiFidelityRepository(scene_repository)
    plan = _plan()
    multifidelity_repository.save_plan(plan)

    task_a = _task(
        plan,
        plan.candidates[0],
        config_char='1',
        estimate_char='2',
    )
    task_b = _task(
        plan,
        plan.candidates[1],
        config_char='1',
        estimate_char='4',
    )
    capacity_ref = _ref('hardware_capacity', 'fixture-capacity', '5')
    capacity = ExecutionCapacityAuthority(
        capacity_authority_ref=capacity_ref,
        capacity=ExecutionResourceVector(
            cpu_threads=4,
            gpu_slots=1,
            memory_bytes=8_000_000,
            scratch_bytes=2_000_000,
        ),
    )
    result_ref = _ref('stage_result', 'coarse-result-a', '6')
    provenance_ref = _ref('execution_provenance', 'execution-a', '7')

    all_refs = (
        *_task_refs(task_a),
        *_task_refs(task_b),
        capacity_ref,
        result_ref,
        provenance_ref,
    )
    values, resolver = _registry(*all_refs)
    repository = CadMultiFidelityExecutionRepository(
        scene_repository,
        multifidelity_repository=multifidelity_repository,
        external_authority_resolver=resolver,
    )
    repository.save_task(task_a)
    repository.save_task(task_b)

    schedule = build_multifidelity_execution_schedule(
        tasks=(task_a, task_b),
        capacity_authority=capacity,
    )
    repository.save_schedule(schedule)

    cache = build_multifidelity_execution_cache_entry(
        task=task_a,
        result_authority_ref=result_ref,
        execution_provenance_ref=provenance_ref,
        completed_at_utc=NOW,
    )
    repository.save_cache_entry(cache)

    reused, pending = repository.partition_resume((task_a, task_b))
    assert reused == (cache,)
    assert pending == (task_b,)

    reopened_scene = SceneRepository(scene_repository.path)
    reopened_multifidelity = CadMultiFidelityRepository(reopened_scene)
    reopened = CadMultiFidelityExecutionRepository(
        reopened_scene,
        multifidelity_repository=reopened_multifidelity,
        external_authority_resolver=resolver,
    )
    assert reopened.get_task(task_a.task_id) == task_a
    assert reopened.get_schedule(schedule.schedule_id) == schedule
    assert reopened.reusable_cache(task_a) == cache

    changed_config = _task(
        plan,
        plan.candidates[0],
        config_char='8',
        estimate_char='2',
    )
    values.update({
        ref.key(): ref for ref in _task_refs(changed_config)
    })
    reopened.save_task(changed_config)
    assert reopened.reusable_cache(changed_config) is None

    values.pop(result_ref.key())
    with pytest.raises(
        ValueError,
        match='execution result exact external authority does not exist',
    ):
        reopened.reusable_cache(task_a)


def test_r140_task_reopen_requires_exact_evaluator_authority(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    multifidelity_repository = CadMultiFidelityRepository(scene_repository)
    plan = _plan()
    multifidelity_repository.save_plan(plan)
    task = _task(
        plan,
        plan.candidates[0],
        config_char='1',
        estimate_char='2',
    )
    values, resolver = _registry(*_task_refs(task))
    repository = CadMultiFidelityExecutionRepository(
        scene_repository,
        multifidelity_repository=multifidelity_repository,
        external_authority_resolver=resolver,
    )
    repository.save_task(task)

    values.pop(task.evaluator_authority.key())
    with pytest.raises(
        ValueError,
        match='stage evaluator exact external authority does not exist',
    ):
        repository.get_task(task.task_id)


def test_r140_resource_vector_requires_compute_slot() -> None:
    with pytest.raises(
        ValueError,
        match='requires CPU threads or GPU slots',
    ):
        ExecutionResourceVector(
            cpu_threads=0,
            gpu_slots=0,
            memory_bytes=1_000_000,
            scratch_bytes=1_000,
        )
