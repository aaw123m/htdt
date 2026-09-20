from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_multifidelity import (
    MultiFidelityAuthorityRef,
    MultiFidelityStageDefinition,
    build_multifidelity_plan,
)
from htdt.cad_r140_executor import ResourceAdmissionError, ResourceQuantity
from htdt.cad_r140_gpu_execution import (
    CadR140GpuAuthorityRepository,
    CpuGpuComparisonTolerance,
    CpuGpuEquivalenceEvaluation,
    GpuExecutionProvenance,
    IdentityDatum,
    build_cpu_gpu_equivalence_spec,
    build_cpu_gpu_observable_evidence,
    build_gpu_execution_capability,
    build_gpu_execution_provenance,
    build_gpu_execution_task_authority,
    build_gpu_multifidelity_execution_task,
    build_gpu_resource_estimate,
    evaluate_cpu_gpu_equivalence,
)
from htdt.cad_repository import SceneRepository


def _ref(kind: str, authority_id: str, char: str) -> MultiFidelityAuthorityRef:
    return MultiFidelityAuthorityRef(
        authority_kind=kind,
        authority_id=authority_id,
        authority_version='1',
        semantic_sha256=char * 64,
    )


SOLVER = _ref('solver_implementation', 'solver-impl', 'a')
GPU_BACKEND = _ref('execution_backend', 'gpu-backend', 'b')
CPU_BACKEND = _ref('execution_backend', 'cpu-backend', 'c')
INPUT = _ref('candidate_wave_execution_input', 'input', 'd')
CPU_RESULT = _ref('solver_result', 'cpu-result', 'e')
GPU_RESULT = _ref('solver_result', 'gpu-result', 'f')
GRID = '1' * 64


def _capability(
    *,
    available: bool = True,
    observables=('complex_pressure', 'magnitude', 'phase', 'scalar_diagnostic'),
):
    if available:
        return build_gpu_execution_capability(
            solver_implementation_ref=SOLVER,
            backend_implementation_ref=GPU_BACKEND,
            gpu_api_backend='CUDA',
            runtime_version=IdentityDatum.known('runtime-1'),
            device_identity=IdentityDatum.known('GPU fixture device'),
            driver_runtime_identity=IdentityDatum.known('driver-1'),
            availability='AVAILABLE',
            supported_precisions=('complex128', 'float64'),
            supported_observables=observables,
        )
    return build_gpu_execution_capability(
        solver_implementation_ref=SOLVER,
        backend_implementation_ref=GPU_BACKEND,
        gpu_api_backend='CUDA',
        runtime_version=IdentityDatum.unavailable(),
        device_identity=IdentityDatum.unavailable(),
        driver_runtime_identity=IdentityDatum.unavailable(),
        availability='UNAVAILABLE',
        reason='no compatible GPU device detected',
    )


def _estimate(capability, *, vram=None, precision='complex128'):
    return build_gpu_resource_estimate(
        capability=capability,
        exact_solver_input_ref=INPUT,
        exact_grid_mesh_sha256=GRID,
        precision=precision,
        gpu_slots=ResourceQuantity.known(1, 'slots'),
        vram_bytes=vram or ResourceQuantity.known(512 * 1024 * 1024, 'bytes'),
        host_ram_bytes=ResourceQuantity.known(256 * 1024 * 1024, 'bytes'),
        cpu_workers=ResourceQuantity.known(1, 'workers'),
        scratch_bytes=ResourceQuantity.known(1024 * 1024, 'bytes'),
        max_concurrent_tasks=ResourceQuantity.known(1, 'tasks'),
        concurrency_constraints=('one task per GPU slot',),
        estimate_method='fixture-declared',
        estimate_method_version='1',
        confidence='HIGH',
    )


def _spec(
    capability,
    *,
    input_ref=INPUT,
    precision='complex128',
    cpu_ref=CPU_RESULT,
    observable='complex_pressure',
    tolerance=None,
):
    if tolerance is None:
        tolerance = (
            CpuGpuComparisonTolerance(phase_absolute_rad=1.0e-6)
            if observable == 'phase'
            else CpuGpuComparisonTolerance(absolute=1.0e-8, relative=1.0e-6)
        )
    return build_cpu_gpu_equivalence_spec(
        capability=capability,
        exact_solver_input_ref=input_ref,
        exact_grid_mesh_sha256=GRID,
        precision=precision,
        cpu_reference_ref=cpu_ref,
        output_observable=observable,
        tolerance=tolerance,
    )


def _task(capability, estimate=None, spec=None):
    estimate = estimate or _estimate(capability)
    spec = spec or _spec(capability, precision=estimate.precision)
    return build_gpu_execution_task_authority(
        capability=capability,
        resource_estimate=estimate,
        equivalence_spec=spec,
    )


def _plan():
    candidates = (
        _ref('system_variant', 'variant-a', '2'),
        _ref('system_variant', 'variant-b', '3'),
    )
    return build_multifidelity_plan(
        domain='o90_robustness',
        name='R140 GPU authority fixture',
        baseline_authority=_ref('scene_revision', 'baseline', '4'),
        candidates=candidates,
        stages=(
            MultiFidelityStageDefinition(
                stage_id='gpu',
                order=0,
                name='GPU validation',
                policy='hard_gate',
                fidelity_label='fixture',
                evaluator_authority=_ref('objective_evaluator', 'gpu-evaluator', '5'),
            ),
        ),
    )


def _evidence(
    *,
    producer,
    hardware,
    result_ref,
    backend_ref,
    input_ref=INPUT,
    precision='complex128',
    observable='complex_pressure',
    real=(1.0, 2.0),
    imag=(0.5, -0.25),
):
    return build_cpu_gpu_observable_evidence(
        producer=producer,
        hardware_evidence=hardware,
        result_authority_ref=result_ref,
        solver_implementation_ref=SOLVER,
        backend_implementation_ref=backend_ref,
        exact_solver_input_ref=input_ref,
        exact_grid_mesh_sha256=GRID,
        precision=precision,
        observable=observable,
        real_values=real,
        imag_values=imag if observable == 'complex_pressure' else (),
        labels=('r0:f0', 'r0:f1'),
    )


def _real_fixture(*, gpu_hardware='REAL_GPU_HARDWARE'):
    capability = _capability()
    estimate = _estimate(capability)
    spec = _spec(capability)
    task = _task(capability, estimate, spec)
    provenance = build_gpu_execution_provenance(
        task=task,
        capability=capability,
        resource_estimate=estimate,
        execution_state='SUCCEEDED',
        hardware_evidence=gpu_hardware,
        result_authority_ref=GPU_RESULT,
    )
    cpu = _evidence(
        producer='CPU_REFERENCE',
        hardware='CPU_EXECUTION',
        result_ref=CPU_RESULT,
        backend_ref=CPU_BACKEND,
    )
    gpu = _evidence(
        producer='GPU_RESULT',
        hardware=gpu_hardware,
        result_ref=GPU_RESULT,
        backend_ref=GPU_BACKEND,
        real=(1.0 + 1.0e-9, 2.0 - 1.0e-9),
        imag=(0.5, -0.25 + 1.0e-9),
    )
    return capability, estimate, spec, task, provenance, cpu, gpu


def test_deterministic_gpu_task_identity_and_generic_scheduler_bridge() -> None:
    capability = _capability()
    estimate = _estimate(capability)
    spec = _spec(capability)

    first = _task(capability, estimate, spec)
    second = _task(capability, estimate, spec)
    assert first == second
    assert first.semantic_sha256 == second.semantic_sha256
    assert first.comparison_tolerance == spec.tolerance

    plan = _plan()
    generic_first = build_gpu_multifidelity_execution_task(
        plan=plan,
        stage_id='gpu',
        candidate=plan.candidates[0],
        capability=capability,
        resource_estimate=estimate,
        gpu_task_authority=first,
    )
    generic_second = build_gpu_multifidelity_execution_task(
        plan=plan,
        stage_id='gpu',
        candidate=plan.candidates[0],
        capability=capability,
        resource_estimate=estimate,
        gpu_task_authority=second,
    )
    assert generic_first == generic_second
    assert generic_first.resource_request.gpu_slots == 1
    assert generic_first.execution_configuration_ref == first.authority_ref()
    assert generic_first.device_refs == (capability.authority_ref(),)


def test_gpu_resource_unknown_is_not_zero_and_fails_closed() -> None:
    capability = _capability()
    unknown = _estimate(
        capability,
        vram=ResourceQuantity.unknown('bytes'),
    )
    assert unknown.vram_bytes.state == 'UNKNOWN'
    with pytest.raises(ResourceAdmissionError) as exc_info:
        unknown.admission_resource_vector()
    assert exc_info.value.state == 'DEFER'

    zero = _estimate(
        capability,
        vram=ResourceQuantity.known(0, 'bytes'),
    )
    assert zero.vram_bytes.state == 'KNOWN'
    with pytest.raises(ResourceAdmissionError) as exc_info:
        zero.admission_resource_vector()
    assert exc_info.value.state == 'REJECT'


def test_cpu_only_capability_does_not_break_cpu_authority_or_create_gpu_task() -> None:
    unavailable = _capability(available=False)
    estimate = _estimate(unavailable)
    spec = _spec(unavailable)
    task = _task(unavailable, estimate, spec)

    assert unavailable.availability == 'UNAVAILABLE'
    with pytest.raises(ResourceAdmissionError) as exc_info:
        build_gpu_multifidelity_execution_task(
            plan=_plan(),
            stage_id='gpu',
            candidate=_plan().candidates[0],
            capability=unavailable,
            resource_estimate=estimate,
            gpu_task_authority=task,
        )
    assert exc_info.value.state == 'REJECT'
    assert CPU_BACKEND.authority_id == 'cpu-backend'


def test_cpu_gpu_comparison_spec_identity_binds_explicit_tolerance() -> None:
    capability = _capability()
    first = _spec(capability)
    second = _spec(capability)
    changed = _spec(
        capability,
        tolerance=CpuGpuComparisonTolerance(absolute=1.0e-7, relative=1.0e-6),
    )

    assert first == second
    assert first.semantic_sha256 == second.semantic_sha256
    assert first.semantic_sha256 != changed.semantic_sha256


def test_real_gpu_complex_pressure_equivalence_uses_explicit_tolerance() -> None:
    capability, _, spec, _, provenance, cpu, gpu = _real_fixture()
    evaluation = evaluate_cpu_gpu_equivalence(
        spec=spec,
        capability=capability,
        cpu=cpu,
        gpu=gpu,
        provenance=provenance,
    )

    assert evaluation.state == 'PASS'
    assert evaluation.gpu_numerical_equivalence_validated is True
    assert evaluation.compared_value_count == 2
    assert evaluation.max_absolute_error is not None
    assert evaluation.production_gpu_support is False


def test_mismatched_solver_input_is_rejected_before_comparison() -> None:
    capability, _, spec, _, provenance, _, gpu = _real_fixture()
    cpu = _evidence(
        producer='CPU_REFERENCE',
        hardware='CPU_EXECUTION',
        result_ref=CPU_RESULT,
        backend_ref=CPU_BACKEND,
        input_ref=_ref('candidate_wave_execution_input', 'changed-input', '6'),
    )

    with pytest.raises(ValueError, match='CPU exact solver input mismatch'):
        evaluate_cpu_gpu_equivalence(
            spec=spec,
            capability=capability,
            cpu=cpu,
            gpu=gpu,
            provenance=provenance,
        )


def test_mismatched_precision_is_rejected_before_comparison() -> None:
    capability, _, spec, _, provenance, _, gpu = _real_fixture()
    cpu = _evidence(
        producer='CPU_REFERENCE',
        hardware='CPU_EXECUTION',
        result_ref=CPU_RESULT,
        backend_ref=CPU_BACKEND,
        precision='float64',
    )

    with pytest.raises(ValueError, match='CPU precision mismatch'):
        evaluate_cpu_gpu_equivalence(
            spec=spec,
            capability=capability,
            cpu=cpu,
            gpu=gpu,
            provenance=provenance,
        )


def test_stale_cpu_reference_is_rejected() -> None:
    capability, _, spec, _, provenance, _, gpu = _real_fixture()
    stale_ref = _ref('solver_result', 'old-cpu-result', '7')
    cpu = _evidence(
        producer='CPU_REFERENCE',
        hardware='CPU_EXECUTION',
        result_ref=stale_ref,
        backend_ref=CPU_BACKEND,
    )

    with pytest.raises(ValueError, match='stale CPU reference rejected'):
        evaluate_cpu_gpu_equivalence(
            spec=spec,
            capability=capability,
            cpu=cpu,
            gpu=gpu,
            provenance=provenance,
        )


def test_mock_gpu_output_cannot_be_promoted_to_numerical_validation() -> None:
    capability, _, spec, _, provenance, cpu, gpu = _real_fixture(
        gpu_hardware='MOCK_GPU'
    )
    assert provenance.validation_eligible is False

    evaluation = evaluate_cpu_gpu_equivalence(
        spec=spec,
        capability=capability,
        cpu=cpu,
        gpu=gpu,
        provenance=provenance,
    )
    assert evaluation.state == 'NOT_VALIDATED'
    assert evaluation.gpu_numerical_equivalence_validated is False
    assert evaluation.production_gpu_support is False

    fabricated = provenance.model_copy(update={'validation_eligible': True})
    with pytest.raises(ValidationError, match='mock/synthetic'):
        GpuExecutionProvenance.model_validate(
            fabricated.model_dump(mode='python')
        )


def test_unsupported_gpu_observable_is_explicit() -> None:
    capability = _capability(observables=('complex_pressure',))
    estimate = _estimate(capability, precision='float64')
    spec = _spec(
        capability,
        precision='float64',
        observable='magnitude',
    )
    task = _task(capability, estimate, spec)
    provenance = build_gpu_execution_provenance(
        task=task,
        capability=capability,
        resource_estimate=estimate,
        execution_state='SUCCEEDED',
        hardware_evidence='REAL_GPU_HARDWARE',
        result_authority_ref=GPU_RESULT,
    )
    cpu = _evidence(
        producer='CPU_REFERENCE',
        hardware='CPU_EXECUTION',
        result_ref=CPU_RESULT,
        backend_ref=CPU_BACKEND,
        precision='float64',
        observable='magnitude',
        real=(1.0, 2.0),
        imag=(),
    )
    gpu = _evidence(
        producer='GPU_RESULT',
        hardware='REAL_GPU_HARDWARE',
        result_ref=GPU_RESULT,
        backend_ref=GPU_BACKEND,
        precision='float64',
        observable='magnitude',
        real=(1.0, 2.0),
        imag=(),
    )

    evaluation = evaluate_cpu_gpu_equivalence(
        spec=spec,
        capability=capability,
        cpu=cpu,
        gpu=gpu,
        provenance=provenance,
    )
    assert evaluation.state == 'UNSUPPORTED'
    assert evaluation.gpu_numerical_equivalence_validated is False


def test_phase_comparison_uses_circular_absolute_tolerance() -> None:
    capability = _capability()
    estimate = _estimate(capability, precision='float64')
    spec = _spec(
        capability,
        precision='float64',
        observable='phase',
        tolerance=CpuGpuComparisonTolerance(phase_absolute_rad=0.02),
    )
    task = _task(capability, estimate, spec)
    provenance = build_gpu_execution_provenance(
        task=task,
        capability=capability,
        resource_estimate=estimate,
        execution_state='SUCCEEDED',
        hardware_evidence='REAL_GPU_HARDWARE',
        result_authority_ref=GPU_RESULT,
    )
    cpu = _evidence(
        producer='CPU_REFERENCE',
        hardware='CPU_EXECUTION',
        result_ref=CPU_RESULT,
        backend_ref=CPU_BACKEND,
        precision='float64',
        observable='phase',
        real=(3.141, -3.141),
        imag=(),
    )
    gpu = _evidence(
        producer='GPU_RESULT',
        hardware='REAL_GPU_HARDWARE',
        result_ref=GPU_RESULT,
        backend_ref=GPU_BACKEND,
        precision='float64',
        observable='phase',
        real=(-3.141, 3.141),
        imag=(),
    )

    evaluation = evaluate_cpu_gpu_equivalence(
        spec=spec,
        capability=capability,
        cpu=cpu,
        gpu=gpu,
        provenance=provenance,
    )
    assert evaluation.state == 'PASS'
    assert evaluation.max_phase_error_rad is not None
    assert evaluation.max_phase_error_rad < 0.01


def test_gpu_authority_persistence_reopens_exactly(tmp_path: Path) -> None:
    capability, estimate, spec, task, provenance, cpu, gpu = _real_fixture()
    evaluation = evaluate_cpu_gpu_equivalence(
        spec=spec,
        capability=capability,
        cpu=cpu,
        gpu=gpu,
        provenance=provenance,
    )

    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    repository = CadR140GpuAuthorityRepository(scene)
    repository.save_capability(capability)
    repository.save_resource_estimate(estimate)
    repository.save_spec(spec)
    repository.save_task(task)
    repository.save_provenance(provenance)
    repository.save_evidence(cpu)
    repository.save_evidence(gpu)
    repository.save_evaluation(evaluation)

    reopened = CadR140GpuAuthorityRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    assert reopened.get_capability(capability.capability_id) == capability
    assert reopened.get_resource_estimate(estimate.estimate_id) == estimate
    assert reopened.get_spec(spec.spec_id) == spec
    assert reopened.get_task(task.gpu_task_id) == task
    assert reopened.get_provenance(provenance.provenance_id) == provenance
    assert reopened.get_evidence(cpu.evidence_id) == cpu
    assert reopened.get_evaluation(evaluation.evaluation_id) == evaluation
