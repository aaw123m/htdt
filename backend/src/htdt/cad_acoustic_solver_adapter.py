from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
import json
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_acoustic_snapshot import (
    AcousticPredictionRequest,
    AcousticSceneSnapshot,
)
from .cad_equipment import FrequencyDomain
from .r120_geometry_compiler import ExactExternalAuthorityRef


ACOUSTIC_SOLVER_ADAPTER_SCHEMA_VERSION = 1
ACOUSTIC_SOLVER_ADAPTER_AUTHORITY_VERSION = '1'
ACOUSTIC_SOLVER_DISPATCH_SCHEMA_VERSION = 2
ACOUSTIC_SOLVER_DISPATCH_AUTHORITY_VERSION = '2'

AcousticSolverDomain = Literal['wave', 'geometric']
SolverDispatchState = Literal['READY', 'BLOCKED', 'UNSUPPORTED']


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _semantic_hash(payload: object) -> str:
    return sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


def _domain_contains(
    container: FrequencyDomain,
    requested: FrequencyDomain,
) -> bool:
    return (
        float(requested.minimum_hz) >= float(container.minimum_hz)
        and float(requested.maximum_hz) <= float(container.maximum_hz)
    )


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


class AcousticSolverAdapterDescriptor(BaseModel):
    """Exact solver-adapter capability authority.

    This is a dispatch contract only. It does not select a production solver and
    it does not execute one.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = ACOUSTIC_SOLVER_ADAPTER_SCHEMA_VERSION
    authority_version: Literal['1'] = ACOUSTIC_SOLVER_ADAPTER_AUTHORITY_VERSION

    descriptor_id: str = Field(
        pattern=r'^acoustic-solver-adapter:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    model_solver_role_id: str = Field(min_length=1)
    acoustic_domain: AcousticSolverDomain

    solver_implementation_ref: ExactExternalAuthorityRef
    solver_configuration_schema_ref: ExactExternalAuthorityRef

    supported_snapshot_schema_versions: tuple[int, ...] = Field(min_length=1)
    supported_observables: tuple[str, ...] = Field(min_length=1)
    valid_frequency_domain: FrequencyDomain

    deterministic_input_binding: Literal[True] = True

    @model_validator(mode='after')
    def validate_descriptor_identity(self) -> 'AcousticSolverAdapterDescriptor':
        if any(version < 1 for version in self.supported_snapshot_schema_versions):
            raise ValueError('supported snapshot schema versions must be positive')
        if self.supported_snapshot_schema_versions != tuple(
            sorted(set(self.supported_snapshot_schema_versions))
        ):
            raise ValueError(
                'supported snapshot schema versions must be unique and sorted'
            )
        if self.supported_observables != tuple(
            sorted(set(self.supported_observables))
        ):
            raise ValueError('supported observables must be unique and sorted')

        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('AcousticSolverAdapterDescriptor semantic hash mismatch')
        if self.descriptor_id != f'acoustic-solver-adapter:{expected}':
            raise ValueError('AcousticSolverAdapterDescriptor id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'descriptor_id', 'semantic_sha256'},
        )


class AcousticNumericalFidelityPolicy(BaseModel):
    """Resolved numerical-fidelity policy authority.

    The policy stays an exact external authority: a resolver must return the
    policy bound to the same authority id/version/hash tuple the prediction
    request declared. HTDT verifies only this declared applicability envelope
    (solver role, acoustic domain, observables, frequency band and declared
    numerical parameter bounds); the numerical solver semantics stay inside
    the hashed external authority payload.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_ref: ExactExternalAuthorityRef
    acoustic_domain: AcousticSolverDomain
    model_solver_role_ids: tuple[str, ...] = Field(min_length=1)
    supported_observables: tuple[str, ...] = Field(min_length=1)
    valid_frequency_domain: FrequencyDomain
    parameter_bounds: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_policy(self) -> 'AcousticNumericalFidelityPolicy':
        if self.model_solver_role_ids != tuple(
            sorted(set(self.model_solver_role_ids))
        ):
            raise ValueError(
                'fidelity policy solver roles must be unique and sorted'
            )
        if self.supported_observables != tuple(
            sorted(set(self.supported_observables))
        ):
            raise ValueError(
                'fidelity policy observables must be unique and sorted'
            )
        return self


NumericalFidelityPolicyResolver = Callable[
    [ExactExternalAuthorityRef],
    AcousticNumericalFidelityPolicy | None,
]


def numerical_fidelity_policy_request_reasons(
    *,
    policy: AcousticNumericalFidelityPolicy,
    request: AcousticPredictionRequest,
) -> tuple[str, ...]:
    """Applicability failures of a resolved fidelity policy for one request."""
    reasons: list[str] = []
    if request.model_solver_role_id not in policy.model_solver_role_ids:
        reasons.append('numerical_fidelity_policy_role_not_applicable')
    reasons.extend(
        f'numerical_fidelity_policy_observable_not_supported:{observable}'
        for observable in request.requested_observables
        if observable not in policy.supported_observables
    )
    if not _domain_contains(
        policy.valid_frequency_domain,
        request.requested_frequency_domain,
    ):
        reasons.append(
            'numerical_fidelity_policy_frequency_domain_not_supported'
        )
    return tuple(reasons)


class AcousticSolverDispatchBinding(BaseModel):
    """Exact request-to-adapter dispatch evaluation.

    READY means the exact immutable request can be handed to the named adapter
    configuration. It does not mean a numerical solve has run or passed.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[2] = ACOUSTIC_SOLVER_DISPATCH_SCHEMA_VERSION
    authority_version: Literal['2'] = ACOUSTIC_SOLVER_DISPATCH_AUTHORITY_VERSION

    binding_id: str = Field(
        pattern=r'^acoustic-solver-dispatch:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    acoustic_scene_snapshot_id: str = Field(
        pattern=r'^acoustic-scene-snapshot:[0-9a-f]{64}$'
    )
    acoustic_scene_snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    prediction_request_id: str = Field(
        pattern=r'^acoustic-prediction-request:[0-9a-f]{64}$'
    )
    prediction_request_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    prediction_deterministic_input_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    adapter_descriptor_id: str = Field(
        pattern=r'^acoustic-solver-adapter:[0-9a-f]{64}$'
    )
    adapter_descriptor_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    solver_implementation_ref: ExactExternalAuthorityRef
    solver_configuration_ref: ExactExternalAuthorityRef
    numerical_fidelity_policy_ref: ExactExternalAuthorityRef

    state: SolverDispatchState
    reasons: tuple[str, ...]
    deterministic_solver_input_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def validate_binding_identity(self) -> 'AcousticSolverDispatchBinding':
        if len(self.reasons) != len(set(self.reasons)):
            raise ValueError('solver dispatch reasons must be unique')
        if self.state == 'READY' and self.reasons:
            raise ValueError('READY solver dispatch cannot carry reasons')
        if self.state != 'READY' and not self.reasons:
            raise ValueError('blocked/unsupported solver dispatch requires reasons')

        expected_solver_input_hash = _semantic_hash(self.solver_input_payload())
        if self.deterministic_solver_input_hash != expected_solver_input_hash:
            raise ValueError('solver dispatch deterministic input hash mismatch')

        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('AcousticSolverDispatchBinding semantic hash mismatch')
        if self.binding_id != f'acoustic-solver-dispatch:{expected}':
            raise ValueError('AcousticSolverDispatchBinding id mismatch')
        return self

    def solver_input_payload(self) -> dict[str, Any]:
        return {
            'prediction_request_id': self.prediction_request_id,
            'prediction_request_semantic_sha256': (
                self.prediction_request_semantic_sha256
            ),
            'prediction_deterministic_input_hash': (
                self.prediction_deterministic_input_hash
            ),
            'adapter_descriptor_id': self.adapter_descriptor_id,
            'adapter_descriptor_semantic_sha256': (
                self.adapter_descriptor_semantic_sha256
            ),
            'solver_implementation_ref': self.solver_implementation_ref.model_dump(
                mode='json'
            ),
            'solver_configuration_ref': self.solver_configuration_ref.model_dump(
                mode='json'
            ),
            'numerical_fidelity_policy_ref': (
                self.numerical_fidelity_policy_ref.model_dump(mode='json')
            ),
        }

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'acoustic_scene_snapshot_id': self.acoustic_scene_snapshot_id,
            'acoustic_scene_snapshot_sha256': (
                self.acoustic_scene_snapshot_sha256
            ),
            'prediction_request_id': self.prediction_request_id,
            'prediction_request_semantic_sha256': (
                self.prediction_request_semantic_sha256
            ),
            'prediction_deterministic_input_hash': (
                self.prediction_deterministic_input_hash
            ),
            'adapter_descriptor_id': self.adapter_descriptor_id,
            'adapter_descriptor_semantic_sha256': (
                self.adapter_descriptor_semantic_sha256
            ),
            'solver_implementation_ref': self.solver_implementation_ref.model_dump(
                mode='json'
            ),
            'solver_configuration_ref': self.solver_configuration_ref.model_dump(
                mode='json'
            ),
            'numerical_fidelity_policy_ref': (
                self.numerical_fidelity_policy_ref.model_dump(mode='json')
            ),
            'state': self.state,
            'reasons': list(self.reasons),
            'deterministic_solver_input_hash': self.deterministic_solver_input_hash,
        }


def build_acoustic_solver_adapter_descriptor(
    *,
    adapter_id: str,
    adapter_version: str,
    model_solver_role_id: str,
    acoustic_domain: AcousticSolverDomain,
    solver_implementation_ref: ExactExternalAuthorityRef,
    solver_configuration_schema_ref: ExactExternalAuthorityRef,
    supported_snapshot_schema_versions: Sequence[int],
    supported_observables: Sequence[str],
    valid_frequency_domain: FrequencyDomain,
) -> AcousticSolverAdapterDescriptor:
    schema_versions = tuple(sorted(set(int(item) for item in supported_snapshot_schema_versions)))
    observables = tuple(sorted(set(str(item) for item in supported_observables)))
    if not schema_versions:
        raise ValueError('solver adapter requires at least one snapshot schema version')
    if not observables:
        raise ValueError('solver adapter requires at least one observable')

    core = {
        'schema_version': ACOUSTIC_SOLVER_ADAPTER_SCHEMA_VERSION,
        'authority_version': ACOUSTIC_SOLVER_ADAPTER_AUTHORITY_VERSION,
        'adapter_id': adapter_id,
        'adapter_version': adapter_version,
        'model_solver_role_id': model_solver_role_id,
        'acoustic_domain': acoustic_domain,
        'solver_implementation_ref': solver_implementation_ref.model_dump(
            mode='json'
        ),
        'solver_configuration_schema_ref': (
            solver_configuration_schema_ref.model_dump(mode='json')
        ),
        'supported_snapshot_schema_versions': list(schema_versions),
        'supported_observables': list(observables),
        'valid_frequency_domain': valid_frequency_domain.model_dump(mode='json'),
        'deterministic_input_binding': True,
    }
    digest = _semantic_hash(core)
    return AcousticSolverAdapterDescriptor(
        descriptor_id=f'acoustic-solver-adapter:{digest}',
        semantic_sha256=digest,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        model_solver_role_id=model_solver_role_id,
        acoustic_domain=acoustic_domain,
        solver_implementation_ref=solver_implementation_ref,
        solver_configuration_schema_ref=solver_configuration_schema_ref,
        supported_snapshot_schema_versions=schema_versions,
        supported_observables=observables,
        valid_frequency_domain=valid_frequency_domain,
    )


def bind_prediction_request_to_solver_adapter(
    *,
    snapshot: AcousticSceneSnapshot,
    request: AcousticPredictionRequest,
    adapter: AcousticSolverAdapterDescriptor,
    solver_configuration_ref: ExactExternalAuthorityRef,
    numerical_fidelity_policy: AcousticNumericalFidelityPolicy,
) -> AcousticSolverDispatchBinding:
    snapshot = AcousticSceneSnapshot.model_validate(
        snapshot.model_dump(mode='python')
    )
    request = AcousticPredictionRequest.model_validate(
        request.model_dump(mode='python')
    )
    adapter = AcousticSolverAdapterDescriptor.model_validate(
        adapter.model_dump(mode='python')
    )
    policy = AcousticNumericalFidelityPolicy.model_validate(
        numerical_fidelity_policy.model_dump(mode='python')
    )

    if (
        request.acoustic_scene_snapshot_id != snapshot.snapshot_id
        or request.acoustic_scene_snapshot_sha256 != snapshot.semantic_sha256
    ):
        raise ValueError(
            'prediction request does not bind the exact AcousticSceneSnapshot'
        )
    if policy.authority_ref != request.numerical_fidelity_policy_ref:
        raise ValueError(
            'numerical fidelity policy does not resolve the exact '
            'AcousticPredictionRequest authority'
        )

    unsupported: list[str] = []
    blocked: list[str] = []

    if request.model_solver_role_id != adapter.model_solver_role_id:
        unsupported.append('model_solver_role_not_supported_by_adapter')

    if snapshot.schema_version not in adapter.supported_snapshot_schema_versions:
        unsupported.append('snapshot_schema_version_not_supported_by_adapter')

    unknown_observables = [
        observable
        for observable in request.requested_observables
        if observable not in adapter.supported_observables
    ]
    if unknown_observables:
        unsupported.extend(
            f'observable_not_supported_by_adapter:{observable}'
            for observable in unknown_observables
        )

    if not _domain_contains(
        adapter.valid_frequency_domain,
        request.requested_frequency_domain,
    ):
        unsupported.append('frequency_domain_not_supported_by_adapter')

    if not _domain_contains(
        snapshot.requested_frequency_domain,
        request.requested_frequency_domain,
    ):
        unsupported.append('frequency_domain_not_declared_by_snapshot')

    if (
        snapshot.valid_frequency_domain is not None
        and not _domain_contains(
            snapshot.valid_frequency_domain,
            request.requested_frequency_domain,
        )
    ):
        unsupported.append('frequency_domain_outside_snapshot_valid_authority')

    if policy.acoustic_domain != adapter.acoustic_domain:
        unsupported.append(
            'numerical_fidelity_policy_domain_not_supported_by_adapter'
        )
    unsupported.extend(
        numerical_fidelity_policy_request_reasons(
            policy=policy,
            request=request,
        )
    )

    readiness = snapshot.readiness
    if not readiness.geometry_ready:
        blocked.append('snapshot_geometry_not_ready')
    if not readiness.receiver_ready:
        blocked.append('snapshot_receiver_not_ready')

    if adapter.acoustic_domain == 'wave':
        if not readiness.wave_source_ready:
            blocked.append('snapshot_wave_source_not_ready')
        if not readiness.wave_boundary_ready:
            blocked.append('snapshot_wave_boundary_not_ready')
        if not readiness.environment_ready:
            blocked.append('snapshot_environment_not_ready')
    else:
        if not readiness.geometric_directivity_ready:
            blocked.append('snapshot_geometric_directivity_not_ready')
        geometric_boundary_ready = (
            readiness.geometric_boundary_ready
            if readiness.geometric_boundary_ready is not None
            else readiness.wave_boundary_ready
        )
        if not geometric_boundary_ready:
            blocked.append('snapshot_geometric_boundary_not_ready')
        if (
            any(
                observable != 'deterministic_paths'
                for observable in request.requested_observables
            )
            and not readiness.environment_ready
        ):
            blocked.append('snapshot_environment_not_ready')

    observable_states = {
        item.observable: item
        for item in readiness.observable_readiness
    }
    for observable in request.requested_observables:
        state = observable_states.get(observable)
        if state is None:
            unsupported.append(
                f'observable_not_declared_in_snapshot_readiness:{observable}'
            )
            continue
        if state.state == 'UNSUPPORTED':
            unsupported.append(
                f'snapshot_observable_unsupported:{observable}'
            )
        elif state.state == 'BLOCKED':
            blocked.append(
                f'snapshot_observable_blocked:{observable}'
            )

    unsupported_reasons = _unique(unsupported)
    blocked_reasons = _unique(blocked)
    if unsupported_reasons:
        state: SolverDispatchState = 'UNSUPPORTED'
        reasons = (*unsupported_reasons, *blocked_reasons)
    elif blocked_reasons:
        state = 'BLOCKED'
        reasons = blocked_reasons
    else:
        state = 'READY'
        reasons = ()

    solver_input_payload = {
        'prediction_request_id': request.request_id,
        'prediction_request_semantic_sha256': request.request_semantic_sha256,
        'prediction_deterministic_input_hash': request.deterministic_input_hash,
        'adapter_descriptor_id': adapter.descriptor_id,
        'adapter_descriptor_semantic_sha256': adapter.semantic_sha256,
        'solver_implementation_ref': adapter.solver_implementation_ref.model_dump(
            mode='json'
        ),
        'solver_configuration_ref': solver_configuration_ref.model_dump(
            mode='json'
        ),
        'numerical_fidelity_policy_ref': policy.authority_ref.model_dump(
            mode='json'
        ),
    }
    solver_input_hash = _semantic_hash(solver_input_payload)

    core = {
        'schema_version': ACOUSTIC_SOLVER_DISPATCH_SCHEMA_VERSION,
        'authority_version': ACOUSTIC_SOLVER_DISPATCH_AUTHORITY_VERSION,
        'acoustic_scene_snapshot_id': snapshot.snapshot_id,
        'acoustic_scene_snapshot_sha256': snapshot.semantic_sha256,
        'prediction_request_id': request.request_id,
        'prediction_request_semantic_sha256': request.request_semantic_sha256,
        'prediction_deterministic_input_hash': request.deterministic_input_hash,
        'adapter_descriptor_id': adapter.descriptor_id,
        'adapter_descriptor_semantic_sha256': adapter.semantic_sha256,
        'solver_implementation_ref': adapter.solver_implementation_ref.model_dump(
            mode='json'
        ),
        'solver_configuration_ref': solver_configuration_ref.model_dump(
            mode='json'
        ),
        'numerical_fidelity_policy_ref': policy.authority_ref.model_dump(
            mode='json'
        ),
        'state': state,
        'reasons': list(reasons),
        'deterministic_solver_input_hash': solver_input_hash,
    }
    digest = _semantic_hash(core)
    return AcousticSolverDispatchBinding(
        binding_id=f'acoustic-solver-dispatch:{digest}',
        semantic_sha256=digest,
        acoustic_scene_snapshot_id=snapshot.snapshot_id,
        acoustic_scene_snapshot_sha256=snapshot.semantic_sha256,
        prediction_request_id=request.request_id,
        prediction_request_semantic_sha256=request.request_semantic_sha256,
        prediction_deterministic_input_hash=request.deterministic_input_hash,
        adapter_descriptor_id=adapter.descriptor_id,
        adapter_descriptor_semantic_sha256=adapter.semantic_sha256,
        solver_implementation_ref=adapter.solver_implementation_ref,
        solver_configuration_ref=solver_configuration_ref,
        numerical_fidelity_policy_ref=policy.authority_ref,
        state=state,
        reasons=tuple(reasons),
        deterministic_solver_input_hash=solver_input_hash,
    )
