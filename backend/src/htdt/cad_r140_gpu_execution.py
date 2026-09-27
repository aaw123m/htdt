from __future__ import annotations

from collections.abc import Sequence
from contextlib import closing
from datetime import datetime, timezone
import math
from pathlib import Path
import sqlite3
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_multifidelity import MultiFidelityAuthorityRef, MultiFidelityPlan
from .cad_multifidelity_execution import (
    ExecutionResourceVector,
    MultiFidelityExecutionTask,
    build_multifidelity_execution_task,
)
from .cad_r140_executor import ResourceAdmissionError, ResourceQuantity
from .cad_repository import SceneRepository
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
)
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest


R140_GPU_SCHEMA_VERSION = 1
GPU_CAPABILITY_AUTHORITY_VERSION = 'r140-gpu-capability-1'
GPU_RESOURCE_ESTIMATE_AUTHORITY_VERSION = 'r140-gpu-resource-estimate-1'
GPU_TASK_AUTHORITY_VERSION = 'r140-gpu-task-1'
GPU_PROVENANCE_AUTHORITY_VERSION = 'r140-gpu-provenance-1'
CPU_GPU_EQUIVALENCE_SPEC_VERSION = 'r140-cpu-gpu-equivalence-spec-1'
CPU_GPU_OBSERVABLE_EVIDENCE_VERSION = 'r140-cpu-gpu-observable-evidence-1'
CPU_GPU_EQUIVALENCE_EVALUATION_VERSION = 'r140-cpu-gpu-equivalence-evaluation-1'

GpuObservable = Literal[
    'complex_pressure',
    'magnitude',
    'phase',
    'scalar_diagnostic',
]
GpuAvailabilityState = Literal['AVAILABLE', 'UNAVAILABLE', 'UNKNOWN']
GpuHardwareEvidence = Literal[
    'NONE',
    'REAL_GPU_HARDWARE',
    'MOCK_GPU',
    'SYNTHETIC_GPU',
]
GpuExecutionState = Literal['NOT_RUN', 'BLOCKED', 'SUCCEEDED', 'FAILED']
EquivalenceState = Literal[
    'PASS',
    'FAIL',
    'BLOCKED',
    'NOT_VALIDATED',
    'UNSUPPORTED',
]






def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class IdentityDatum(BaseModel):
    """String identity that preserves UNKNOWN/UNAVAILABLE separately from values."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    state: Literal['KNOWN', 'UNKNOWN', 'UNAVAILABLE']
    value: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def validate_state(self) -> 'IdentityDatum':
        if self.state == 'KNOWN' and self.value is None:
            raise ValueError('KNOWN identity datum requires a value')
        if self.state != 'KNOWN' and self.value is not None:
            raise ValueError('UNKNOWN/UNAVAILABLE identity datum cannot carry a value')
        return self

    @classmethod
    def known(cls, value: str) -> 'IdentityDatum':
        return cls(state='KNOWN', value=value)

    @classmethod
    def unknown(cls) -> 'IdentityDatum':
        return cls(state='UNKNOWN')

    @classmethod
    def unavailable(cls) -> 'IdentityDatum':
        return cls(state='UNAVAILABLE')


class GpuExecutionCapability(BaseModel):
    """Exact capability observation for one GPU backend/runtime/device identity."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = R140_GPU_SCHEMA_VERSION
    authority_version: Literal[
        'r140-gpu-capability-1'
    ] = GPU_CAPABILITY_AUTHORITY_VERSION
    capability_id: str = Field(pattern=r'^r140-gpu-capability:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    solver_implementation_ref: MultiFidelityAuthorityRef
    backend_implementation_ref: MultiFidelityAuthorityRef
    gpu_api_backend: str = Field(min_length=1)
    runtime_version: IdentityDatum
    device_identity: IdentityDatum
    driver_runtime_identity: IdentityDatum
    availability: GpuAvailabilityState
    supported_precisions: tuple[str, ...] = ()
    supported_observables: tuple[GpuObservable, ...] = ()
    reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def validate_capability(self) -> 'GpuExecutionCapability':
        if len(self.supported_precisions) != len(set(self.supported_precisions)):
            raise ValueError('GPU supported precisions must be unique')
        if len(self.supported_observables) != len(set(self.supported_observables)):
            raise ValueError('GPU supported observables must be unique')
        if self.availability == 'AVAILABLE':
            for label, datum in (
                ('runtime version', self.runtime_version),
                ('device identity', self.device_identity),
                ('driver/runtime identity', self.driver_runtime_identity),
            ):
                if datum.state != 'KNOWN':
                    raise ValueError(f'AVAILABLE GPU requires KNOWN {label}')
            if not self.supported_precisions:
                raise ValueError('AVAILABLE GPU requires supported precision authority')
            if not self.supported_observables:
                raise ValueError('AVAILABLE GPU requires supported observable authority')
        elif self.reason is None:
            raise ValueError('unavailable/unknown GPU capability requires a reason')

        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('GpuExecutionCapability semantic hash mismatch')
        if self.capability_id != f'r140-gpu-capability:{expected}':
            raise ValueError('GpuExecutionCapability id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'capability_id', 'semantic_sha256'},
        )

    def authority_ref(self) -> MultiFidelityAuthorityRef:
        return MultiFidelityAuthorityRef(
            authority_kind='gpu_execution_capability',
            authority_id=self.capability_id,
            authority_version=self.authority_version,
            semantic_sha256=self.semantic_sha256,
        )


def build_gpu_execution_capability(
    *,
    solver_implementation_ref: MultiFidelityAuthorityRef,
    backend_implementation_ref: MultiFidelityAuthorityRef,
    gpu_api_backend: str,
    runtime_version: IdentityDatum,
    device_identity: IdentityDatum,
    driver_runtime_identity: IdentityDatum,
    availability: GpuAvailabilityState,
    supported_precisions: Sequence[str] = (),
    supported_observables: Sequence[GpuObservable] = (),
    reason: str | None = None,
) -> GpuExecutionCapability:
    core = {
        'schema_version': R140_GPU_SCHEMA_VERSION,
        'authority_version': GPU_CAPABILITY_AUTHORITY_VERSION,
        'solver_implementation_ref': solver_implementation_ref.model_dump(mode='json'),
        'backend_implementation_ref': backend_implementation_ref.model_dump(mode='json'),
        'gpu_api_backend': gpu_api_backend,
        'runtime_version': runtime_version.model_dump(mode='json'),
        'device_identity': device_identity.model_dump(mode='json'),
        'driver_runtime_identity': driver_runtime_identity.model_dump(mode='json'),
        'availability': availability,
        'supported_precisions': list(supported_precisions),
        'supported_observables': list(supported_observables),
        'reason': reason,
    }
    digest = _digest(core)
    return GpuExecutionCapability(
        capability_id=f'r140-gpu-capability:{digest}',
        semantic_sha256=digest,
        **core,
    )


class GpuResourceEstimate(BaseModel):
    """GPU resource authority with fail-closed UNKNOWN semantics."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = R140_GPU_SCHEMA_VERSION
    authority_version: Literal[
        'r140-gpu-resource-estimate-1'
    ] = GPU_RESOURCE_ESTIMATE_AUTHORITY_VERSION
    estimate_id: str = Field(
        pattern=r'^r140-gpu-resource-estimate:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    capability_ref: MultiFidelityAuthorityRef
    solver_implementation_ref: MultiFidelityAuthorityRef
    backend_implementation_ref: MultiFidelityAuthorityRef
    exact_solver_input_ref: MultiFidelityAuthorityRef
    exact_grid_mesh_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    precision: str = Field(min_length=1)

    gpu_slots: ResourceQuantity
    vram_bytes: ResourceQuantity
    host_ram_bytes: ResourceQuantity
    cpu_workers: ResourceQuantity
    scratch_bytes: ResourceQuantity
    max_concurrent_tasks: ResourceQuantity
    concurrency_constraints: tuple[str, ...] = ()

    estimate_method: str = Field(min_length=1)
    estimate_method_version: str = Field(min_length=1)
    confidence: Literal['HIGH', 'MEDIUM', 'LOW', 'UNKNOWN']
    assumptions: tuple[str, ...] = ()

    @model_validator(mode='after')
    def validate_identity(self) -> 'GpuResourceEstimate':
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('GpuResourceEstimate semantic hash mismatch')
        if self.estimate_id != f'r140-gpu-resource-estimate:{expected}':
            raise ValueError('GpuResourceEstimate id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'estimate_id', 'semantic_sha256'},
        )

    def authority_ref(self) -> MultiFidelityAuthorityRef:
        return MultiFidelityAuthorityRef(
            authority_kind='gpu_resource_estimate',
            authority_id=self.estimate_id,
            authority_version=self.authority_version,
            semantic_sha256=self.semantic_sha256,
        )

    def admission_resource_vector(self) -> ExecutionResourceVector:
        gpu_slots = self.gpu_slots.require('GPU slot requirement')
        vram = self.vram_bytes.require('VRAM requirement')
        host_ram = self.host_ram_bytes.require('host RAM requirement')
        cpu_workers = self.cpu_workers.require('CPU worker requirement')
        scratch = self.scratch_bytes.require('scratch requirement')
        concurrency = self.max_concurrent_tasks.require(
            'GPU concurrency constraint'
        )
        if gpu_slots <= 0:
            raise ResourceAdmissionError(
                'GPU execution requires at least one GPU slot',
                state='REJECT',
            )
        if vram <= 0:
            raise ResourceAdmissionError(
                'GPU execution requires positive VRAM capacity',
                state='REJECT',
            )
        if cpu_workers <= 0:
            raise ResourceAdmissionError(
                'GPU execution requires at least one host CPU worker',
                state='REJECT',
            )
        if concurrency <= 0:
            raise ResourceAdmissionError(
                'GPU max concurrent tasks must be positive',
                state='REJECT',
            )
        return ExecutionResourceVector(
            cpu_threads=cpu_workers,
            gpu_slots=gpu_slots,
            memory_bytes=host_ram,
            scratch_bytes=scratch,
        )


def build_gpu_resource_estimate(
    *,
    capability: GpuExecutionCapability,
    exact_solver_input_ref: MultiFidelityAuthorityRef,
    exact_grid_mesh_sha256: str,
    precision: str,
    gpu_slots: ResourceQuantity,
    vram_bytes: ResourceQuantity,
    host_ram_bytes: ResourceQuantity,
    cpu_workers: ResourceQuantity,
    scratch_bytes: ResourceQuantity,
    max_concurrent_tasks: ResourceQuantity,
    concurrency_constraints: Sequence[str] = (),
    estimate_method: str,
    estimate_method_version: str,
    confidence: Literal['HIGH', 'MEDIUM', 'LOW', 'UNKNOWN'],
    assumptions: Sequence[str] = (),
) -> GpuResourceEstimate:
    core = {
        'schema_version': R140_GPU_SCHEMA_VERSION,
        'authority_version': GPU_RESOURCE_ESTIMATE_AUTHORITY_VERSION,
        'capability_ref': capability.authority_ref().model_dump(mode='json'),
        'solver_implementation_ref': capability.solver_implementation_ref.model_dump(
            mode='json'
        ),
        'backend_implementation_ref': capability.backend_implementation_ref.model_dump(
            mode='json'
        ),
        'exact_solver_input_ref': exact_solver_input_ref.model_dump(mode='json'),
        'exact_grid_mesh_sha256': exact_grid_mesh_sha256,
        'precision': precision,
        'gpu_slots': gpu_slots.model_dump(mode='json'),
        'vram_bytes': vram_bytes.model_dump(mode='json'),
        'host_ram_bytes': host_ram_bytes.model_dump(mode='json'),
        'cpu_workers': cpu_workers.model_dump(mode='json'),
        'scratch_bytes': scratch_bytes.model_dump(mode='json'),
        'max_concurrent_tasks': max_concurrent_tasks.model_dump(mode='json'),
        'concurrency_constraints': list(concurrency_constraints),
        'estimate_method': estimate_method,
        'estimate_method_version': estimate_method_version,
        'confidence': confidence,
        'assumptions': list(assumptions),
    }
    digest = _digest(core)
    return GpuResourceEstimate(
        estimate_id=f'r140-gpu-resource-estimate:{digest}',
        semantic_sha256=digest,
        **core,
    )


class CpuGpuComparisonTolerance(BaseModel):
    """Explicit observable tolerance. Never auto-relaxed by backend."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    absolute: float = Field(default=0.0, ge=0.0)
    relative: float = Field(default=0.0, ge=0.0)
    relative_floor: float = Field(default=1.0e-30, gt=0.0)
    phase_absolute_rad: float | None = Field(default=None, ge=0.0)

    @model_validator(mode='after')
    def validate_tolerance(self) -> 'CpuGpuComparisonTolerance':
        values = (self.absolute, self.relative, self.relative_floor)
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError('comparison tolerance values must be finite')
        if self.phase_absolute_rad is not None and not math.isfinite(
            float(self.phase_absolute_rad)
        ):
            raise ValueError('phase tolerance must be finite')
        return self


class CpuGpuEquivalenceSpec(BaseModel):
    """Exact comparison identity for CPU reference versus one GPU backend."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = R140_GPU_SCHEMA_VERSION
    authority_version: Literal[
        'r140-cpu-gpu-equivalence-spec-1'
    ] = CPU_GPU_EQUIVALENCE_SPEC_VERSION
    spec_id: str = Field(pattern=r'^r140-cpu-gpu-equivalence-spec:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    solver_implementation_ref: MultiFidelityAuthorityRef
    gpu_backend_implementation_ref: MultiFidelityAuthorityRef
    capability_ref: MultiFidelityAuthorityRef
    gpu_api_backend: str = Field(min_length=1)
    exact_solver_input_ref: MultiFidelityAuthorityRef
    exact_grid_mesh_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    precision: str = Field(min_length=1)
    cpu_reference_ref: MultiFidelityAuthorityRef
    output_observable: GpuObservable
    tolerance: CpuGpuComparisonTolerance

    @model_validator(mode='after')
    def validate_spec(self) -> 'CpuGpuEquivalenceSpec':
        if self.output_observable == 'phase':
            if self.tolerance.phase_absolute_rad is None:
                raise ValueError('phase observable requires explicit phase tolerance')
        elif self.tolerance.phase_absolute_rad is not None:
            raise ValueError('phase tolerance is only valid for phase observable')
        if self.output_observable != 'phase':
            if self.tolerance.absolute == 0.0 and self.tolerance.relative == 0.0:
                raise ValueError(
                    'numeric equivalence requires non-zero explicit tolerance'
                )
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('CpuGpuEquivalenceSpec semantic hash mismatch')
        if self.spec_id != f'r140-cpu-gpu-equivalence-spec:{expected}':
            raise ValueError('CpuGpuEquivalenceSpec id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'spec_id', 'semantic_sha256'},
        )

    def authority_ref(self) -> MultiFidelityAuthorityRef:
        return MultiFidelityAuthorityRef(
            authority_kind='cpu_gpu_equivalence_spec',
            authority_id=self.spec_id,
            authority_version=self.authority_version,
            semantic_sha256=self.semantic_sha256,
        )


def build_cpu_gpu_equivalence_spec(
    *,
    capability: GpuExecutionCapability,
    exact_solver_input_ref: MultiFidelityAuthorityRef,
    exact_grid_mesh_sha256: str,
    precision: str,
    cpu_reference_ref: MultiFidelityAuthorityRef,
    output_observable: GpuObservable,
    tolerance: CpuGpuComparisonTolerance,
) -> CpuGpuEquivalenceSpec:
    core = {
        'schema_version': R140_GPU_SCHEMA_VERSION,
        'authority_version': CPU_GPU_EQUIVALENCE_SPEC_VERSION,
        'solver_implementation_ref': capability.solver_implementation_ref.model_dump(
            mode='json'
        ),
        'gpu_backend_implementation_ref': capability.backend_implementation_ref.model_dump(
            mode='json'
        ),
        'capability_ref': capability.authority_ref().model_dump(mode='json'),
        'gpu_api_backend': capability.gpu_api_backend,
        'exact_solver_input_ref': exact_solver_input_ref.model_dump(mode='json'),
        'exact_grid_mesh_sha256': exact_grid_mesh_sha256,
        'precision': precision,
        'cpu_reference_ref': cpu_reference_ref.model_dump(mode='json'),
        'output_observable': output_observable,
        'tolerance': tolerance.model_dump(mode='json'),
    }
    digest = _digest(core)
    return CpuGpuEquivalenceSpec(
        spec_id=f'r140-cpu-gpu-equivalence-spec:{digest}',
        semantic_sha256=digest,
        **core,
    )


class GpuExecutionTaskAuthority(BaseModel):
    """GPU-specific configuration identity embedded into the generic R140 task."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = R140_GPU_SCHEMA_VERSION
    authority_version: Literal['r140-gpu-task-1'] = GPU_TASK_AUTHORITY_VERSION
    gpu_task_id: str = Field(pattern=r'^r140-gpu-task:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    solver_implementation_ref: MultiFidelityAuthorityRef
    backend_implementation_ref: MultiFidelityAuthorityRef
    capability_ref: MultiFidelityAuthorityRef
    resource_estimate_ref: MultiFidelityAuthorityRef
    equivalence_spec_ref: MultiFidelityAuthorityRef
    gpu_api_backend: str = Field(min_length=1)
    runtime_version: IdentityDatum
    device_identity: IdentityDatum
    driver_runtime_identity: IdentityDatum
    exact_solver_input_ref: MultiFidelityAuthorityRef
    exact_grid_mesh_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    precision: str = Field(min_length=1)
    cpu_reference_ref: MultiFidelityAuthorityRef
    output_observable: GpuObservable
    comparison_tolerance: CpuGpuComparisonTolerance

    @model_validator(mode='after')
    def validate_identity(self) -> 'GpuExecutionTaskAuthority':
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('GpuExecutionTaskAuthority semantic hash mismatch')
        if self.gpu_task_id != f'r140-gpu-task:{expected}':
            raise ValueError('GpuExecutionTaskAuthority id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'gpu_task_id', 'semantic_sha256'},
        )

    def authority_ref(self) -> MultiFidelityAuthorityRef:
        return MultiFidelityAuthorityRef(
            authority_kind='gpu_execution_task',
            authority_id=self.gpu_task_id,
            authority_version=self.authority_version,
            semantic_sha256=self.semantic_sha256,
        )


def build_gpu_execution_task_authority(
    *,
    capability: GpuExecutionCapability,
    resource_estimate: GpuResourceEstimate,
    equivalence_spec: CpuGpuEquivalenceSpec,
) -> GpuExecutionTaskAuthority:
    if resource_estimate.capability_ref != capability.authority_ref():
        raise ValueError('GPU resource estimate capability mismatch')
    if resource_estimate.solver_implementation_ref != capability.solver_implementation_ref:
        raise ValueError('GPU resource estimate solver implementation mismatch')
    if (
        resource_estimate.backend_implementation_ref
        != capability.backend_implementation_ref
    ):
        raise ValueError('GPU resource estimate backend implementation mismatch')
    if equivalence_spec.capability_ref != capability.authority_ref():
        raise ValueError('CPU/GPU equivalence spec capability mismatch')
    if (
        equivalence_spec.exact_solver_input_ref
        != resource_estimate.exact_solver_input_ref
    ):
        raise ValueError('CPU/GPU equivalence spec solver input mismatch')
    if (
        equivalence_spec.exact_grid_mesh_sha256
        != resource_estimate.exact_grid_mesh_sha256
    ):
        raise ValueError('CPU/GPU equivalence spec grid/mesh mismatch')
    if equivalence_spec.precision != resource_estimate.precision:
        raise ValueError('CPU/GPU equivalence spec precision mismatch')

    core = {
        'schema_version': R140_GPU_SCHEMA_VERSION,
        'authority_version': GPU_TASK_AUTHORITY_VERSION,
        'solver_implementation_ref': capability.solver_implementation_ref.model_dump(
            mode='json'
        ),
        'backend_implementation_ref': capability.backend_implementation_ref.model_dump(
            mode='json'
        ),
        'capability_ref': capability.authority_ref().model_dump(mode='json'),
        'resource_estimate_ref': resource_estimate.authority_ref().model_dump(
            mode='json'
        ),
        'equivalence_spec_ref': equivalence_spec.authority_ref().model_dump(
            mode='json'
        ),
        'gpu_api_backend': capability.gpu_api_backend,
        'runtime_version': capability.runtime_version.model_dump(mode='json'),
        'device_identity': capability.device_identity.model_dump(mode='json'),
        'driver_runtime_identity': capability.driver_runtime_identity.model_dump(
            mode='json'
        ),
        'exact_solver_input_ref': resource_estimate.exact_solver_input_ref.model_dump(
            mode='json'
        ),
        'exact_grid_mesh_sha256': resource_estimate.exact_grid_mesh_sha256,
        'precision': resource_estimate.precision,
        'cpu_reference_ref': equivalence_spec.cpu_reference_ref.model_dump(mode='json'),
        'output_observable': equivalence_spec.output_observable,
        'comparison_tolerance': equivalence_spec.tolerance.model_dump(mode='json'),
    }
    digest = _digest(core)
    return GpuExecutionTaskAuthority(
        gpu_task_id=f'r140-gpu-task:{digest}',
        semantic_sha256=digest,
        **core,
    )


def build_gpu_multifidelity_execution_task(
    *,
    plan: MultiFidelityPlan,
    stage_id: str,
    candidate: MultiFidelityAuthorityRef,
    capability: GpuExecutionCapability,
    resource_estimate: GpuResourceEstimate,
    gpu_task_authority: GpuExecutionTaskAuthority,
) -> MultiFidelityExecutionTask:
    """Bridge GPU authority into the existing deterministic R140 scheduler."""

    if capability.availability != 'AVAILABLE':
        raise ResourceAdmissionError(
            'GPU capability is not AVAILABLE; CPU path may remain usable',
            state='DEFER' if capability.availability == 'UNKNOWN' else 'REJECT',
        )
    if capability.authority_ref() != gpu_task_authority.capability_ref:
        raise ValueError('GPU task capability authority mismatch')
    if resource_estimate.authority_ref() != gpu_task_authority.resource_estimate_ref:
        raise ValueError('GPU task resource estimate authority mismatch')
    if resource_estimate.precision not in capability.supported_precisions:
        raise ResourceAdmissionError(
            'GPU task precision is not supported by exact capability',
            state='REJECT',
        )
    if gpu_task_authority.output_observable not in capability.supported_observables:
        raise ResourceAdmissionError(
            'GPU task observable is not supported by exact capability',
            state='REJECT',
        )

    return build_multifidelity_execution_task(
        plan=plan,
        stage_id=stage_id,
        candidate=candidate,
        execution_backend_ref=capability.backend_implementation_ref,
        execution_configuration_ref=gpu_task_authority.authority_ref(),
        resource_estimate_ref=resource_estimate.authority_ref(),
        device_refs=(capability.authority_ref(),),
        resource_request=resource_estimate.admission_resource_vector(),
    )


class GpuExecutionProvenance(BaseModel):
    """Execution evidence; validation eligibility is impossible for mock output."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = R140_GPU_SCHEMA_VERSION
    authority_version: Literal[
        'r140-gpu-provenance-1'
    ] = GPU_PROVENANCE_AUTHORITY_VERSION
    provenance_id: str = Field(pattern=r'^r140-gpu-provenance:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    gpu_task_ref: MultiFidelityAuthorityRef
    capability_ref: MultiFidelityAuthorityRef
    resource_estimate_ref: MultiFidelityAuthorityRef
    solver_implementation_ref: MultiFidelityAuthorityRef
    backend_implementation_ref: MultiFidelityAuthorityRef
    gpu_api_backend: str = Field(min_length=1)
    runtime_version: IdentityDatum
    device_identity: IdentityDatum
    driver_runtime_identity: IdentityDatum
    exact_solver_input_ref: MultiFidelityAuthorityRef
    exact_grid_mesh_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    precision: str = Field(min_length=1)
    execution_state: GpuExecutionState
    hardware_evidence: GpuHardwareEvidence
    result_authority_ref: MultiFidelityAuthorityRef | None = None
    execution_attempt_ref: MultiFidelityAuthorityRef | None = None
    validation_eligible: bool
    production_gpu_support: Literal[False] = False
    reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def validate_provenance(self) -> 'GpuExecutionProvenance':
        if self.execution_state == 'SUCCEEDED' and self.result_authority_ref is None:
            raise ValueError('successful GPU execution requires result authority')
        if self.execution_state != 'SUCCEEDED' and self.validation_eligible:
            raise ValueError('non-successful GPU execution cannot be validation eligible')
        if self.validation_eligible:
            if self.hardware_evidence != 'REAL_GPU_HARDWARE':
                raise ValueError('mock/synthetic GPU output cannot be validation evidence')
            if self.result_authority_ref is None:
                raise ValueError('validation-eligible GPU execution requires result')
            for label, datum in (
                ('runtime version', self.runtime_version),
                ('device identity', self.device_identity),
                ('driver/runtime identity', self.driver_runtime_identity),
            ):
                if datum.state != 'KNOWN':
                    raise ValueError(
                        f'validation-eligible GPU execution requires KNOWN {label}'
                    )
        if self.hardware_evidence in ('MOCK_GPU', 'SYNTHETIC_GPU') and self.validation_eligible:
            raise ValueError('fabricated GPU output cannot be validation eligible')
        if self.execution_state in ('NOT_RUN', 'BLOCKED') and self.reason is None:
            raise ValueError('non-run/blocked GPU provenance requires reason')

        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('GpuExecutionProvenance semantic hash mismatch')
        if self.provenance_id != f'r140-gpu-provenance:{expected}':
            raise ValueError('GpuExecutionProvenance id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'provenance_id', 'semantic_sha256'},
        )

    def authority_ref(self) -> MultiFidelityAuthorityRef:
        return MultiFidelityAuthorityRef(
            authority_kind='gpu_execution_provenance',
            authority_id=self.provenance_id,
            authority_version=self.authority_version,
            semantic_sha256=self.semantic_sha256,
        )


def build_gpu_execution_provenance(
    *,
    task: GpuExecutionTaskAuthority,
    capability: GpuExecutionCapability,
    resource_estimate: GpuResourceEstimate,
    execution_state: GpuExecutionState,
    hardware_evidence: GpuHardwareEvidence,
    result_authority_ref: MultiFidelityAuthorityRef | None = None,
    execution_attempt_ref: MultiFidelityAuthorityRef | None = None,
    reason: str | None = None,
) -> GpuExecutionProvenance:
    if task.capability_ref != capability.authority_ref():
        raise ValueError('GPU provenance capability mismatch')
    if task.resource_estimate_ref != resource_estimate.authority_ref():
        raise ValueError('GPU provenance resource estimate mismatch')
    validation_eligible = (
        execution_state == 'SUCCEEDED'
        and hardware_evidence == 'REAL_GPU_HARDWARE'
        and result_authority_ref is not None
    )
    core = {
        'schema_version': R140_GPU_SCHEMA_VERSION,
        'authority_version': GPU_PROVENANCE_AUTHORITY_VERSION,
        'gpu_task_ref': task.authority_ref().model_dump(mode='json'),
        'capability_ref': capability.authority_ref().model_dump(mode='json'),
        'resource_estimate_ref': resource_estimate.authority_ref().model_dump(
            mode='json'
        ),
        'solver_implementation_ref': task.solver_implementation_ref.model_dump(
            mode='json'
        ),
        'backend_implementation_ref': task.backend_implementation_ref.model_dump(
            mode='json'
        ),
        'gpu_api_backend': task.gpu_api_backend,
        'runtime_version': task.runtime_version.model_dump(mode='json'),
        'device_identity': task.device_identity.model_dump(mode='json'),
        'driver_runtime_identity': task.driver_runtime_identity.model_dump(
            mode='json'
        ),
        'exact_solver_input_ref': task.exact_solver_input_ref.model_dump(mode='json'),
        'exact_grid_mesh_sha256': task.exact_grid_mesh_sha256,
        'precision': task.precision,
        'execution_state': execution_state,
        'hardware_evidence': hardware_evidence,
        'result_authority_ref': (
            result_authority_ref.model_dump(mode='json')
            if result_authority_ref is not None
            else None
        ),
        'execution_attempt_ref': (
            execution_attempt_ref.model_dump(mode='json')
            if execution_attempt_ref is not None
            else None
        ),
        'validation_eligible': validation_eligible,
        'production_gpu_support': False,
        'reason': reason,
    }
    digest = _digest(core)
    return GpuExecutionProvenance(
        provenance_id=f'r140-gpu-provenance:{digest}',
        semantic_sha256=digest,
        **core,
    )


class CpuGpuObservableEvidence(BaseModel):
    """Typed numerical values bound to exact CPU/GPU result identity."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = R140_GPU_SCHEMA_VERSION
    authority_version: Literal[
        'r140-cpu-gpu-observable-evidence-1'
    ] = CPU_GPU_OBSERVABLE_EVIDENCE_VERSION
    evidence_id: str = Field(
        pattern=r'^r140-cpu-gpu-observable-evidence:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    producer: Literal['CPU_REFERENCE', 'GPU_RESULT']
    hardware_evidence: Literal[
        'CPU_EXECUTION',
        'REAL_GPU_HARDWARE',
        'MOCK_GPU',
        'SYNTHETIC_GPU',
    ]
    result_authority_ref: MultiFidelityAuthorityRef
    solver_implementation_ref: MultiFidelityAuthorityRef
    backend_implementation_ref: MultiFidelityAuthorityRef
    exact_solver_input_ref: MultiFidelityAuthorityRef
    exact_grid_mesh_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    precision: str = Field(min_length=1)
    observable: GpuObservable
    real_values: tuple[float, ...] = Field(min_length=1)
    imag_values: tuple[float, ...] = ()
    labels: tuple[str, ...] = ()

    @model_validator(mode='after')
    def validate_values(self) -> 'CpuGpuObservableEvidence':
        if not all(math.isfinite(float(value)) for value in self.real_values):
            raise ValueError('observable evidence real values must be finite')
        if not all(math.isfinite(float(value)) for value in self.imag_values):
            raise ValueError('observable evidence imaginary values must be finite')
        if self.observable == 'complex_pressure':
            if len(self.real_values) != len(self.imag_values):
                raise ValueError('complex pressure real/imag shape mismatch')
        elif self.imag_values:
            raise ValueError('non-complex observable cannot carry imaginary values')
        if self.observable == 'magnitude' and any(
            value < 0.0 for value in self.real_values
        ):
            raise ValueError('magnitude evidence cannot be negative')
        if self.labels and len(self.labels) != len(self.real_values):
            raise ValueError('observable labels/value shape mismatch')
        if self.producer == 'CPU_REFERENCE':
            if self.hardware_evidence != 'CPU_EXECUTION':
                raise ValueError('CPU reference evidence must come from CPU execution')
        elif self.hardware_evidence == 'CPU_EXECUTION':
            raise ValueError('GPU evidence cannot claim CPU execution origin')

        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('CpuGpuObservableEvidence semantic hash mismatch')
        if self.evidence_id != f'r140-cpu-gpu-observable-evidence:{expected}':
            raise ValueError('CpuGpuObservableEvidence id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'evidence_id', 'semantic_sha256'},
        )

    def authority_ref(self) -> MultiFidelityAuthorityRef:
        return MultiFidelityAuthorityRef(
            authority_kind='cpu_gpu_observable_evidence',
            authority_id=self.evidence_id,
            authority_version=self.authority_version,
            semantic_sha256=self.semantic_sha256,
        )


def build_cpu_gpu_observable_evidence(
    *,
    producer: Literal['CPU_REFERENCE', 'GPU_RESULT'],
    hardware_evidence: Literal[
        'CPU_EXECUTION',
        'REAL_GPU_HARDWARE',
        'MOCK_GPU',
        'SYNTHETIC_GPU',
    ],
    result_authority_ref: MultiFidelityAuthorityRef,
    solver_implementation_ref: MultiFidelityAuthorityRef,
    backend_implementation_ref: MultiFidelityAuthorityRef,
    exact_solver_input_ref: MultiFidelityAuthorityRef,
    exact_grid_mesh_sha256: str,
    precision: str,
    observable: GpuObservable,
    real_values: Sequence[float],
    imag_values: Sequence[float] = (),
    labels: Sequence[str] = (),
) -> CpuGpuObservableEvidence:
    core = {
        'schema_version': R140_GPU_SCHEMA_VERSION,
        'authority_version': CPU_GPU_OBSERVABLE_EVIDENCE_VERSION,
        'producer': producer,
        'hardware_evidence': hardware_evidence,
        'result_authority_ref': result_authority_ref.model_dump(mode='json'),
        'solver_implementation_ref': solver_implementation_ref.model_dump(mode='json'),
        'backend_implementation_ref': backend_implementation_ref.model_dump(mode='json'),
        'exact_solver_input_ref': exact_solver_input_ref.model_dump(mode='json'),
        'exact_grid_mesh_sha256': exact_grid_mesh_sha256,
        'precision': precision,
        'observable': observable,
        'real_values': [float(item) for item in real_values],
        'imag_values': [float(item) for item in imag_values],
        'labels': list(labels),
    }
    digest = _digest(core)
    return CpuGpuObservableEvidence(
        evidence_id=f'r140-cpu-gpu-observable-evidence:{digest}',
        semantic_sha256=digest,
        **core,
    )


class CpuGpuEquivalenceEvaluation(BaseModel):
    """Immutable mechanical decision. PASS/FAIL requires real GPU evidence."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = R140_GPU_SCHEMA_VERSION
    authority_version: Literal[
        'r140-cpu-gpu-equivalence-evaluation-1'
    ] = CPU_GPU_EQUIVALENCE_EVALUATION_VERSION
    evaluation_id: str = Field(
        pattern=r'^r140-cpu-gpu-equivalence-evaluation:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    spec_ref: MultiFidelityAuthorityRef
    cpu_evidence_ref: MultiFidelityAuthorityRef
    gpu_evidence_ref: MultiFidelityAuthorityRef
    gpu_provenance_ref: MultiFidelityAuthorityRef
    state: EquivalenceState
    gpu_hardware_evidence: GpuHardwareEvidence
    compared_value_count: int = Field(ge=0)
    max_absolute_error: float | None = Field(default=None, ge=0.0)
    max_relative_error: float | None = Field(default=None, ge=0.0)
    max_phase_error_rad: float | None = Field(default=None, ge=0.0)
    reason_codes: tuple[str, ...]
    gpu_numerical_equivalence_validated: bool
    production_gpu_support: Literal[False] = False

    @model_validator(mode='after')
    def validate_decision(self) -> 'CpuGpuEquivalenceEvaluation':
        if self.state in ('PASS', 'FAIL'):
            if self.gpu_hardware_evidence != 'REAL_GPU_HARDWARE':
                raise ValueError(
                    'PASS/FAIL CPU/GPU equivalence requires real GPU hardware'
                )
            if self.compared_value_count <= 0:
                raise ValueError('PASS/FAIL requires compared numerical values')
        if self.state == 'PASS':
            if not self.gpu_numerical_equivalence_validated:
                raise ValueError('PASS requires validated numerical equivalence')
        elif self.gpu_numerical_equivalence_validated:
            raise ValueError(
                'only PASS may claim GPU numerical equivalence validated'
            )
        if self.gpu_hardware_evidence in ('MOCK_GPU', 'SYNTHETIC_GPU'):
            if self.state in ('PASS', 'FAIL'):
                raise ValueError('fabricated GPU validation state rejected')

        for value in (
            self.max_absolute_error,
            self.max_relative_error,
            self.max_phase_error_rad,
        ):
            if value is not None and not math.isfinite(float(value)):
                raise ValueError('equivalence metric must be finite')

        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('CpuGpuEquivalenceEvaluation semantic hash mismatch')
        if (
            self.evaluation_id
            != f'r140-cpu-gpu-equivalence-evaluation:{expected}'
        ):
            raise ValueError('CpuGpuEquivalenceEvaluation id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'evaluation_id', 'semantic_sha256'},
        )

    def authority_ref(self) -> MultiFidelityAuthorityRef:
        return MultiFidelityAuthorityRef(
            authority_kind='cpu_gpu_equivalence_evaluation',
            authority_id=self.evaluation_id,
            authority_version=self.authority_version,
            semantic_sha256=self.semantic_sha256,
        )


def _evaluation(
    *,
    spec: CpuGpuEquivalenceSpec,
    cpu: CpuGpuObservableEvidence,
    gpu: CpuGpuObservableEvidence,
    provenance: GpuExecutionProvenance,
    state: EquivalenceState,
    compared_value_count: int,
    gpu_hardware_evidence: GpuHardwareEvidence,
    reason_codes: Sequence[str],
    max_absolute_error: float | None = None,
    max_relative_error: float | None = None,
    max_phase_error_rad: float | None = None,
) -> CpuGpuEquivalenceEvaluation:
    validated = state == 'PASS'
    core = {
        'schema_version': R140_GPU_SCHEMA_VERSION,
        'authority_version': CPU_GPU_EQUIVALENCE_EVALUATION_VERSION,
        'spec_ref': spec.authority_ref().model_dump(mode='json'),
        'cpu_evidence_ref': cpu.authority_ref().model_dump(mode='json'),
        'gpu_evidence_ref': gpu.authority_ref().model_dump(mode='json'),
        'gpu_provenance_ref': provenance.authority_ref().model_dump(mode='json'),
        'state': state,
        'gpu_hardware_evidence': gpu_hardware_evidence,
        'compared_value_count': compared_value_count,
        'max_absolute_error': max_absolute_error,
        'max_relative_error': max_relative_error,
        'max_phase_error_rad': max_phase_error_rad,
        'reason_codes': list(reason_codes),
        'gpu_numerical_equivalence_validated': validated,
        'production_gpu_support': False,
    }
    digest = _digest(core)
    return CpuGpuEquivalenceEvaluation(
        evaluation_id=f'r140-cpu-gpu-equivalence-evaluation:{digest}',
        semantic_sha256=digest,
        **core,
    )


def evaluate_cpu_gpu_equivalence(
    *,
    spec: CpuGpuEquivalenceSpec,
    capability: GpuExecutionCapability,
    cpu: CpuGpuObservableEvidence,
    gpu: CpuGpuObservableEvidence,
    provenance: GpuExecutionProvenance,
) -> CpuGpuEquivalenceEvaluation:
    """Evaluate exact CPU/GPU evidence or fail closed before numeric comparison."""

    if spec.capability_ref != capability.authority_ref():
        raise ValueError('CPU/GPU equivalence capability is stale')
    if cpu.producer != 'CPU_REFERENCE':
        raise ValueError('CPU/GPU equivalence requires CPU reference evidence')
    if gpu.producer != 'GPU_RESULT':
        raise ValueError('CPU/GPU equivalence requires GPU result evidence')
    if cpu.result_authority_ref != spec.cpu_reference_ref:
        raise ValueError('stale CPU reference rejected')
    if cpu.solver_implementation_ref != spec.solver_implementation_ref:
        raise ValueError('CPU reference solver implementation mismatch')
    if gpu.solver_implementation_ref != spec.solver_implementation_ref:
        raise ValueError('GPU solver implementation mismatch')
    if gpu.backend_implementation_ref != spec.gpu_backend_implementation_ref:
        raise ValueError('GPU backend implementation mismatch')

    for evidence, label in ((cpu, 'CPU'), (gpu, 'GPU')):
        if evidence.exact_solver_input_ref != spec.exact_solver_input_ref:
            raise ValueError(f'{label} exact solver input mismatch')
        if evidence.exact_grid_mesh_sha256 != spec.exact_grid_mesh_sha256:
            raise ValueError(f'{label} exact grid/mesh mismatch')
        if evidence.precision != spec.precision:
            raise ValueError(f'{label} precision mismatch')
        if evidence.observable != spec.output_observable:
            raise ValueError(f'{label} observable mismatch')

    if provenance.capability_ref != capability.authority_ref():
        raise ValueError('GPU provenance capability mismatch')
    if provenance.exact_solver_input_ref != spec.exact_solver_input_ref:
        raise ValueError('GPU provenance exact solver input mismatch')
    if provenance.exact_grid_mesh_sha256 != spec.exact_grid_mesh_sha256:
        raise ValueError('GPU provenance exact grid/mesh mismatch')
    if provenance.precision != spec.precision:
        raise ValueError('GPU provenance precision mismatch')
    if provenance.result_authority_ref != gpu.result_authority_ref:
        raise ValueError('GPU provenance/result authority mismatch')

    if spec.output_observable not in capability.supported_observables:
        return _evaluation(
            spec=spec,
            cpu=cpu,
            gpu=gpu,
            provenance=provenance,
            state='UNSUPPORTED',
            compared_value_count=0,
            gpu_hardware_evidence=provenance.hardware_evidence,
            reason_codes=('GPU_OBSERVABLE_UNSUPPORTED',),
        )
    if spec.precision not in capability.supported_precisions:
        return _evaluation(
            spec=spec,
            cpu=cpu,
            gpu=gpu,
            provenance=provenance,
            state='UNSUPPORTED',
            compared_value_count=0,
            gpu_hardware_evidence=provenance.hardware_evidence,
            reason_codes=('GPU_PRECISION_UNSUPPORTED',),
        )

    if capability.availability != 'AVAILABLE':
        return _evaluation(
            spec=spec,
            cpu=cpu,
            gpu=gpu,
            provenance=provenance,
            state='BLOCKED',
            compared_value_count=0,
            gpu_hardware_evidence=provenance.hardware_evidence,
            reason_codes=('GPU_CAPABILITY_NOT_AVAILABLE',),
        )
    if not provenance.validation_eligible:
        return _evaluation(
            spec=spec,
            cpu=cpu,
            gpu=gpu,
            provenance=provenance,
            state='NOT_VALIDATED',
            compared_value_count=0,
            gpu_hardware_evidence=provenance.hardware_evidence,
            reason_codes=('GPU_REAL_HARDWARE_EVIDENCE_REQUIRED',),
        )
    if gpu.hardware_evidence != 'REAL_GPU_HARDWARE':
        return _evaluation(
            spec=spec,
            cpu=cpu,
            gpu=gpu,
            provenance=provenance,
            state='NOT_VALIDATED',
            compared_value_count=0,
            gpu_hardware_evidence=provenance.hardware_evidence,
            reason_codes=('FABRICATED_GPU_OUTPUT_NOT_NUMERICAL_EVIDENCE',),
        )

    if len(cpu.real_values) != len(gpu.real_values):
        raise ValueError('CPU/GPU observable shape mismatch')
    if cpu.labels != gpu.labels:
        raise ValueError('CPU/GPU observable label/order mismatch')

    tolerance = spec.tolerance
    if spec.output_observable == 'complex_pressure':
        if len(cpu.imag_values) != len(gpu.imag_values):
            raise ValueError('CPU/GPU complex observable shape mismatch')
        absolute_errors: list[float] = []
        relative_errors: list[float] = []
        passed = True
        for cr, ci, gr, gi in zip(
            cpu.real_values,
            cpu.imag_values,
            gpu.real_values,
            gpu.imag_values,
            strict=True,
        ):
            reference = complex(cr, ci)
            actual = complex(gr, gi)
            error = abs(actual - reference)
            denominator = max(abs(reference), tolerance.relative_floor)
            relative = error / denominator
            threshold = tolerance.absolute + tolerance.relative * denominator
            absolute_errors.append(error)
            relative_errors.append(relative)
            passed = passed and error <= threshold
        return _evaluation(
            spec=spec,
            cpu=cpu,
            gpu=gpu,
            provenance=provenance,
            state='PASS' if passed else 'FAIL',
            compared_value_count=len(absolute_errors),
            gpu_hardware_evidence=provenance.hardware_evidence,
            max_absolute_error=max(absolute_errors),
            max_relative_error=max(relative_errors),
            reason_codes=(
                'WITHIN_EXPLICIT_TOLERANCE'
                if passed
                else 'EXPLICIT_TOLERANCE_EXCEEDED',
            ),
        )

    if spec.output_observable == 'phase':
        assert tolerance.phase_absolute_rad is not None
        errors = [
            abs(math.atan2(math.sin(g - c), math.cos(g - c)))
            for c, g in zip(cpu.real_values, gpu.real_values, strict=True)
        ]
        passed = all(error <= tolerance.phase_absolute_rad for error in errors)
        return _evaluation(
            spec=spec,
            cpu=cpu,
            gpu=gpu,
            provenance=provenance,
            state='PASS' if passed else 'FAIL',
            compared_value_count=len(errors),
            gpu_hardware_evidence=provenance.hardware_evidence,
            max_phase_error_rad=max(errors),
            reason_codes=(
                'WITHIN_EXPLICIT_TOLERANCE'
                if passed
                else 'EXPLICIT_TOLERANCE_EXCEEDED',
            ),
        )

    absolute_errors = [
        abs(g - c) for c, g in zip(cpu.real_values, gpu.real_values, strict=True)
    ]
    relative_errors = [
        error / max(abs(reference), tolerance.relative_floor)
        for error, reference in zip(
            absolute_errors,
            cpu.real_values,
            strict=True,
        )
    ]
    passed = all(
        error
        <= tolerance.absolute
        + tolerance.relative * max(abs(reference), tolerance.relative_floor)
        for error, reference in zip(
            absolute_errors,
            cpu.real_values,
            strict=True,
        )
    )
    return _evaluation(
        spec=spec,
        cpu=cpu,
        gpu=gpu,
        provenance=provenance,
        state='PASS' if passed else 'FAIL',
        compared_value_count=len(absolute_errors),
        gpu_hardware_evidence=provenance.hardware_evidence,
        max_absolute_error=max(absolute_errors),
        max_relative_error=max(relative_errors),
        reason_codes=(
            'WITHIN_EXPLICIT_TOLERANCE'
            if passed
            else 'EXPLICIT_TOLERANCE_EXCEEDED',
        ),
    )


T = TypeVar('T', bound=BaseModel)


class CadR140GpuAuthorityRepository:
    """Append-only persistence for GPU/equivalence authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = Path(scene_repository.path)
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
            require_native_tables(connection, 'cad_r140_gpu_authorities')

    def _save(
        self,
        *,
        kind: str,
        authority_id: str,
        semantic_sha256: str,
        model: BaseModel,
    ) -> None:
        payload = model.model_dump_json()
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT semantic_sha256, payload_json
                FROM cad_r140_gpu_authorities
                WHERE authority_kind=? AND authority_id=?
                """,
                (kind, authority_id),
            ).fetchone()
            if row is not None:
                if (
                    row['semantic_sha256'] != semantic_sha256
                    or row['payload_json'] != payload
                ):
                    raise ValueError(
                        'R140 GPU authority id exists with different semantics'
                    )
                return
            connection.execute(
                """
                INSERT INTO cad_r140_gpu_authorities(
                    authority_kind,
                    authority_id,
                    semantic_sha256,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    authority_id,
                    semantic_sha256,
                    payload,
                    _utc_now(),
                ),
            )

    def _get(
        self,
        *,
        kind: str,
        authority_id: str,
        model_type: type[T],
    ) -> T | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_r140_gpu_authorities
                WHERE authority_kind=? AND authority_id=?
                """,
                (kind, authority_id),
            ).fetchone()
        if row is None:
            return None
        return model_type.model_validate_json(row['payload_json'])

    def save_capability(
        self, value: GpuExecutionCapability
    ) -> GpuExecutionCapability:
        value = GpuExecutionCapability.model_validate(value.model_dump(mode='python'))
        self._save(
            kind='gpu_execution_capability',
            authority_id=value.capability_id,
            semantic_sha256=value.semantic_sha256,
            model=value,
        )
        return value

    def get_capability(self, authority_id: str) -> GpuExecutionCapability | None:
        return self._get(
            kind='gpu_execution_capability',
            authority_id=authority_id,
            model_type=GpuExecutionCapability,
        )

    def save_resource_estimate(
        self, value: GpuResourceEstimate
    ) -> GpuResourceEstimate:
        value = GpuResourceEstimate.model_validate(value.model_dump(mode='python'))
        self._save(
            kind='gpu_resource_estimate',
            authority_id=value.estimate_id,
            semantic_sha256=value.semantic_sha256,
            model=value,
        )
        return value

    def get_resource_estimate(self, authority_id: str) -> GpuResourceEstimate | None:
        return self._get(
            kind='gpu_resource_estimate',
            authority_id=authority_id,
            model_type=GpuResourceEstimate,
        )

    def save_task(self, value: GpuExecutionTaskAuthority) -> GpuExecutionTaskAuthority:
        value = GpuExecutionTaskAuthority.model_validate(
            value.model_dump(mode='python')
        )
        self._save(
            kind='gpu_execution_task',
            authority_id=value.gpu_task_id,
            semantic_sha256=value.semantic_sha256,
            model=value,
        )
        return value

    def get_task(self, authority_id: str) -> GpuExecutionTaskAuthority | None:
        return self._get(
            kind='gpu_execution_task',
            authority_id=authority_id,
            model_type=GpuExecutionTaskAuthority,
        )

    def save_provenance(
        self, value: GpuExecutionProvenance
    ) -> GpuExecutionProvenance:
        value = GpuExecutionProvenance.model_validate(
            value.model_dump(mode='python')
        )
        self._save(
            kind='gpu_execution_provenance',
            authority_id=value.provenance_id,
            semantic_sha256=value.semantic_sha256,
            model=value,
        )
        return value

    def get_provenance(self, authority_id: str) -> GpuExecutionProvenance | None:
        return self._get(
            kind='gpu_execution_provenance',
            authority_id=authority_id,
            model_type=GpuExecutionProvenance,
        )

    def save_spec(self, value: CpuGpuEquivalenceSpec) -> CpuGpuEquivalenceSpec:
        value = CpuGpuEquivalenceSpec.model_validate(value.model_dump(mode='python'))
        self._save(
            kind='cpu_gpu_equivalence_spec',
            authority_id=value.spec_id,
            semantic_sha256=value.semantic_sha256,
            model=value,
        )
        return value

    def get_spec(self, authority_id: str) -> CpuGpuEquivalenceSpec | None:
        return self._get(
            kind='cpu_gpu_equivalence_spec',
            authority_id=authority_id,
            model_type=CpuGpuEquivalenceSpec,
        )

    def save_evidence(
        self, value: CpuGpuObservableEvidence
    ) -> CpuGpuObservableEvidence:
        value = CpuGpuObservableEvidence.model_validate(
            value.model_dump(mode='python')
        )
        self._save(
            kind='cpu_gpu_observable_evidence',
            authority_id=value.evidence_id,
            semantic_sha256=value.semantic_sha256,
            model=value,
        )
        return value

    def get_evidence(self, authority_id: str) -> CpuGpuObservableEvidence | None:
        return self._get(
            kind='cpu_gpu_observable_evidence',
            authority_id=authority_id,
            model_type=CpuGpuObservableEvidence,
        )

    def save_evaluation(
        self, value: CpuGpuEquivalenceEvaluation
    ) -> CpuGpuEquivalenceEvaluation:
        value = CpuGpuEquivalenceEvaluation.model_validate(
            value.model_dump(mode='python')
        )
        self._save(
            kind='cpu_gpu_equivalence_evaluation',
            authority_id=value.evaluation_id,
            semantic_sha256=value.semantic_sha256,
            model=value,
        )
        return value

    def get_evaluation(
        self, authority_id: str
    ) -> CpuGpuEquivalenceEvaluation | None:
        return self._get(
            kind='cpu_gpu_equivalence_evaluation',
            authority_id=authority_id,
            model_type=CpuGpuEquivalenceEvaluation,
        )
