from __future__ import annotations

from hashlib import sha256
import json

import pytest

from scripts.run_r130a_candidate_wave_execution import _fixture

from htdt.cad_candidate_wave_execution import (
    CandidateResourceConfiguration,
    CandidateWaveExecutionInput,
    PffdtdCandidateConfiguration,
    build_pffdtd_candidate_configuration,
)
from htdt.cad_multifidelity import (
    MultiFidelityAuthorityRef,
    MultiFidelityStageDefinition,
    build_multifidelity_plan,
)
from htdt.cad_multifidelity_execution import (
    ExecutionCapacityAuthority,
    build_multifidelity_execution_schedule,
    build_multifidelity_execution_task,
)
from htdt.cad_pffdtd_resource_estimator import (
    PFFDTD_RESOURCE_ESTIMATOR_ID,
    PFFDTD_RESOURCE_ESTIMATOR_VERSION,
    PffdtdCandidateResourceEstimator,
    pffdtd_candidate_input_ref,
)
from htdt.cad_r140_executor import ResourceAdmissionError


def _digest(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


def _ref(kind: str, char: str) -> MultiFidelityAuthorityRef:
    return MultiFidelityAuthorityRef(
        authority_kind=kind,
        authority_id=f'{kind}:{char}',
        authority_version='1',
        semantic_sha256=char * 64,
    )


@pytest.fixture(scope='module')
def compiled_candidate(tmp_path_factory):
    root = tmp_path_factory.mktemp('r140-pffdtd-estimator')
    fixture = _fixture(root, root / 'unused-upstream')
    authority, model = fixture['executor'].compile_input(
        dispatch_binding_id=fixture['dispatch'].binding_id,
        configuration=fixture['configuration'],
    )
    return fixture, authority, model


def _configuration(
    original: PffdtdCandidateConfiguration,
    *,
    fmax_hz: float | None = None,
    solver_threads: int | None = None,
    setup_processes: int | None = None,
) -> PffdtdCandidateConfiguration:
    resource = original.resource
    changed_resource = CandidateResourceConfiguration(
        solver_threads=(
            resource.solver_threads
            if solver_threads is None
            else solver_threads
        ),
        setup_processes=(
            resource.setup_processes
            if setup_processes is None
            else setup_processes
        ),
        max_grid_cells=resource.max_grid_cells,
        max_time_steps=resource.max_time_steps,
        max_output_bytes=resource.max_output_bytes,
        max_solver_wall_seconds=resource.max_solver_wall_seconds,
    )
    return build_pffdtd_candidate_configuration(
        expected_pffdtd_commit_sha=original.expected_pffdtd_commit_sha,
        fmax_hz=original.fmax_hz if fmax_hz is None else fmax_hz,
        points_per_wavelength=original.points_per_wavelength,
        duration_s=original.duration_s,
        frequency_samples_hz=original.frequency_samples_hz,
        density_kg_m3=original.density_kg_m3,
        density_authority_ref=original.density_authority_ref,
        relative_humidity_percent=original.relative_humidity_percent,
        humidity_authority_ref=original.humidity_authority_ref,
        resource=changed_resource,
    )


def _rebind_configuration(
    authority: CandidateWaveExecutionInput,
    configuration: PffdtdCandidateConfiguration,
) -> CandidateWaveExecutionInput:
    core = authority.semantic_payload()
    core['solver_configuration_ref'] = (
        configuration.as_external_ref().model_dump(mode='json')
    )
    core['resource_configuration'] = configuration.resource.model_dump(mode='json')
    digest = _digest(core)
    return CandidateWaveExecutionInput(
        execution_input_id=f'candidate-wave-input:{digest}',
        semantic_sha256=digest,
        **core,
    )


def _estimate(compiled_candidate, *, configuration=None, authority=None):
    fixture, exact_input, model = compiled_candidate
    configuration = configuration or fixture['configuration']
    authority = authority or exact_input
    return PffdtdCandidateResourceEstimator().estimate(
        authority=authority,
        model=model,
        configuration=configuration,
        sound_speed_m_s=fixture['snapshot'].environment.sound_speed_m_s,
    )


def _task_for(estimation, candidate):
    evaluator = _ref('evaluator', 'e')
    plan = build_multifidelity_plan(
        domain='o90_robustness',
        name='R140 PFFDTD resource admission fixture',
        baseline_authority=_ref('baseline', 'a'),
        candidates=(candidate, _ref('candidate', 'b')),
        stages=(
            MultiFidelityStageDefinition(
                stage_id='execute',
                order=0,
                name='Execute exact candidate',
                policy='shortlist_only',
                fidelity_label='candidate',
                evaluator_authority=evaluator,
            ),
            MultiFidelityStageDefinition(
                stage_id='final',
                order=1,
                name='Final placeholder',
                policy='final_common_fidelity',
                fidelity_label='candidate',
                evaluator_authority=evaluator,
            ),
        ),
    )
    estimate = estimation.execution_resource_estimate
    return build_multifidelity_execution_task(
        plan=plan,
        stage_id='execute',
        candidate=candidate,
        execution_backend_ref=estimate.execution_backend_ref,
        execution_configuration_ref=estimate.execution_configuration_ref,
        resource_estimate_ref=estimate.authority_ref(),
        resource_request=estimate.admission_resource_vector(),
    )


def _capacity(vector, *, cpu=None, memory=None, scratch=None):
    return ExecutionCapacityAuthority(
        capacity_authority_ref=_ref('capacity', 'c'),
        capacity=vector.model_copy(
            update={
                'cpu_threads': vector.cpu_threads if cpu is None else cpu,
                'memory_bytes': (
                    vector.memory_bytes if memory is None else memory
                ),
                'scratch_bytes': (
                    vector.scratch_bytes if scratch is None else scratch
                ),
            }
        ),
    )


def test_same_exact_input_has_same_estimate_identity(compiled_candidate) -> None:
    first = _estimate(compiled_candidate)
    second = _estimate(compiled_candidate)

    assert first == second
    assert (
        first.execution_resource_estimate.estimate_method
        == PFFDTD_RESOURCE_ESTIMATOR_ID
    )
    assert (
        first.execution_resource_estimate.estimate_method_version
        == PFFDTD_RESOURCE_ESTIMATOR_VERSION
    )
    assert first.workload.grid_cells > 0
    assert first.workload.time_step_count > 0
    assert first.workload.cell_time_updates == (
        first.workload.grid_cells * first.workload.time_step_count
    )


def test_grid_configuration_change_changes_estimate(compiled_candidate) -> None:
    fixture, authority, _ = compiled_candidate
    changed_configuration = _configuration(
        fixture['configuration'],
        fmax_hz=120.0,
    )
    changed_authority = _rebind_configuration(
        authority,
        changed_configuration,
    )

    original = _estimate(compiled_candidate)
    changed = _estimate(
        compiled_candidate,
        configuration=changed_configuration,
        authority=changed_authority,
    )

    assert changed.workload.grid_shape != original.workload.grid_shape
    assert changed.workload.grid_cells != original.workload.grid_cells
    assert (
        changed.execution_resource_estimate.estimate_id
        != original.execution_resource_estimate.estimate_id
    )


def test_thread_change_updates_scheduler_demand(compiled_candidate) -> None:
    fixture, authority, _ = compiled_candidate
    changed_configuration = _configuration(
        fixture['configuration'],
        solver_threads=2,
    )
    changed_authority = _rebind_configuration(
        authority,
        changed_configuration,
    )
    changed = _estimate(
        compiled_candidate,
        configuration=changed_configuration,
        authority=changed_authority,
    )

    vector = changed.execution_resource_estimate.admission_resource_vector()
    assert changed.workload.inner_solver_threads == 2
    assert changed.workload.logical_cpu_demand == 2
    assert vector.cpu_threads == 2


def test_known_ram_scratch_and_cpu_shortage_reject_admission(
    compiled_candidate,
) -> None:
    estimation = _estimate(compiled_candidate)
    candidate = pffdtd_candidate_input_ref(compiled_candidate[1])
    task = _task_for(estimation, candidate)
    vector = task.resource_request

    schedule = build_multifidelity_execution_schedule(
        tasks=(task,),
        capacity_authority=_capacity(vector),
    )
    assert len(schedule.batches) == 1

    with pytest.raises(ValueError, match='exceeds declared capacity'):
        build_multifidelity_execution_schedule(
            tasks=(task,),
            capacity_authority=_capacity(
                vector,
                memory=vector.memory_bytes - 1,
            ),
        )
    with pytest.raises(ValueError, match='exceeds declared capacity'):
        build_multifidelity_execution_schedule(
            tasks=(task,),
            capacity_authority=_capacity(
                vector,
                scratch=vector.scratch_bytes - 1,
            ),
        )
    with pytest.raises(ValueError, match='exceeds declared capacity'):
        build_multifidelity_execution_schedule(
            tasks=(task,),
            capacity_authority=_capacity(
                vector,
                cpu=vector.cpu_threads - 1,
            ),
        )


def test_unknown_multiprocess_ram_is_not_zero(compiled_candidate) -> None:
    fixture, authority, _ = compiled_candidate
    configuration = _configuration(
        fixture['configuration'],
        setup_processes=2,
    )
    rebound = _rebind_configuration(authority, configuration)
    estimation = _estimate(
        compiled_candidate,
        configuration=configuration,
        authority=rebound,
    )

    assert estimation.workload.peak_memory_bytes.state == 'UNKNOWN'
    assert estimation.workload.peak_memory_bytes.value is None
    with pytest.raises(ResourceAdmissionError) as error:
        estimation.execution_resource_estimate.admission_resource_vector()
    assert error.value.state == 'DEFER'


def test_cpu_only_gpu_semantics_are_explicit(compiled_candidate) -> None:
    estimation = _estimate(compiled_candidate)

    assert estimation.workload.gpu_slots.state == 'KNOWN'
    assert estimation.workload.gpu_slots.value == 0
    assert estimation.workload.vram_bytes.state == 'UNAVAILABLE'
    assert estimation.execution_resource_estimate.gpu_slots.value == 0
    assert estimation.execution_resource_estimate.vram_bytes.state == 'UNAVAILABLE'


def test_frequency_dependent_mixed_boundary_has_conservative_resource_estimate(
    tmp_path,
) -> None:
    fixture = _fixture(
        tmp_path / 'r130c-estimator',
        tmp_path / 'unused-upstream',
        boundary_mode='causal',
        fixture_id='r130c-estimator-unit-v1',
    )
    authority, model = fixture['executor'].compile_input(
        dispatch_binding_id=fixture['dispatch'].binding_id,
        configuration=fixture['configuration'],
    )
    estimation = PffdtdCandidateResourceEstimator().estimate(
        authority=authority,
        model=model,
        configuration=fixture['configuration'],
        sound_speed_m_s=fixture['snapshot'].environment.sound_speed_m_s,
    )

    assert authority.authority_version == 'r130c-candidate-wave-input-1'
    assert any(
        item.causal_mapping is not None for item in authority.boundary_bindings
    )
    assert estimation.workload.grid_cells > 0
    assert estimation.workload.peak_memory_bytes.state == 'KNOWN'
    assert any(
        item.component
        == 'material_coefficients_and_lossy_boundary_state_upper_bound'
        for item in estimation.workload.ram_components
    )
    assert any(
        item.component == 'frequency_dependent_material_hdf5_upper_bound'
        for item in estimation.workload.scratch_components
    )
