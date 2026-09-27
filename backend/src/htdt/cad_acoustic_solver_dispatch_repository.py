from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from .cad_acoustic_snapshot import AcousticPredictionRequest
from .cad_acoustic_snapshot_repository import (
    AcousticSnapshotAuthorityResolvers,
    CadAcousticSnapshotRepository,
)
from .cad_acoustic_solver_adapter import (
    AcousticNumericalFidelityPolicy,
    AcousticSolverAdapterDescriptor,
    AcousticSolverDispatchBinding,
    NumericalFidelityPolicyResolver,
    bind_prediction_request_to_solver_adapter,
)
from .cad_repository import SceneRepository
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
    connect_sqlite,

)
from .r120_geometry_compiler import ExactExternalAuthorityRef


ExternalAuthorityResolver = Callable[
    [ExactExternalAuthorityRef],
    ExactExternalAuthorityRef | None,
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CadAcousticSolverDispatchRepository:
    """Append-only solver-adapter and dispatch authority persistence.

    Solver implementation/configuration authorities are exact external inputs.
    They must be re-resolved by a caller-provided authority resolver; an opaque
    stored hash alone is not treated as proof that the external authority still
    exists with the same semantics.

    The request numerical-fidelity policy is resolved the same way through a
    caller-provided fidelity-policy resolver, so a READY dispatch always proves
    the exact resolved fidelity authority.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        snapshot_repository: CadAcousticSnapshotRepository | None = None,
        external_authority_resolver: ExternalAuthorityResolver,
        fidelity_policy_resolver: NumericalFidelityPolicyResolver,
        snapshot_authority_resolvers: AcousticSnapshotAuthorityResolvers | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.fidelity_policy_resolver = fidelity_policy_resolver
        self.snapshot_repository = (
            snapshot_repository
            if snapshot_repository is not None
            else CadAcousticSnapshotRepository(
                scene_repository,
                fidelity_policy_resolver=fidelity_policy_resolver,
                authority_resolvers=snapshot_authority_resolvers,
            )
        )
        if self.snapshot_repository.fidelity_policy_resolver is None:
            raise ValueError(
                'solver dispatch requires a snapshot repository with '
                'numerical fidelity policy resolution'
            )
        self.external_authority_resolver = external_authority_resolver
        self.path = Path(scene_repository.path)
        if Path(self.snapshot_repository.path) != self.path:
            raise ValueError(
                'solver dispatch and acoustic snapshot repositories must share '
                'one native CAD database'
            )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_acoustic_solver_adapters', 'cad_acoustic_solver_dispatch_bindings')

    def _resolve_external_ref(
        self,
        ref: ExactExternalAuthorityRef,
        *,
        label: str,
    ) -> ExactExternalAuthorityRef:
        resolved = self.external_authority_resolver(ref)
        if resolved is None:
            raise ValueError(f'{label} exact external authority does not exist')
        if resolved != ref:
            raise ValueError(f'{label} exact external authority mismatch')
        return resolved

    def _resolve_fidelity_policy(
        self,
        request: AcousticPredictionRequest,
    ) -> AcousticNumericalFidelityPolicy:
        resolved = self.fidelity_policy_resolver(
            request.numerical_fidelity_policy_ref
        )
        if resolved is None:
            raise ValueError(
                'numerical fidelity policy exact external authority '
                'does not exist'
            )
        policy = AcousticNumericalFidelityPolicy.model_validate(
            resolved.model_dump(mode='python')
        )
        if policy.authority_ref != request.numerical_fidelity_policy_ref:
            raise ValueError(
                'numerical fidelity policy exact external authority mismatch'
            )
        return policy

    def _validate_descriptor(
        self,
        descriptor: AcousticSolverAdapterDescriptor,
    ) -> AcousticSolverAdapterDescriptor:
        descriptor = AcousticSolverAdapterDescriptor.model_validate(
            descriptor.model_dump(mode='python')
        )
        self._resolve_external_ref(
            descriptor.solver_implementation_ref,
            label='solver implementation',
        )
        self._resolve_external_ref(
            descriptor.solver_configuration_schema_ref,
            label='solver configuration schema',
        )
        return descriptor

    def save_descriptor(
        self,
        descriptor: AcousticSolverAdapterDescriptor,
    ) -> AcousticSolverAdapterDescriptor:
        descriptor = self._validate_descriptor(descriptor)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_solver_adapters
                WHERE descriptor_id=?
                """,
                (descriptor.descriptor_id,),
            ).fetchone()
            if existing is not None:
                persisted = AcousticSolverAdapterDescriptor.model_validate_json(
                    existing['payload_json']
                )
                if persisted != descriptor:
                    raise ValueError(
                        'solver adapter descriptor id exists with different semantics'
                    )
                return self._validate_descriptor(persisted)
            connection.execute(
                """
                INSERT INTO cad_acoustic_solver_adapters(
                    descriptor_id,
                    semantic_sha256,
                    adapter_id,
                    adapter_version,
                    model_solver_role_id,
                    acoustic_domain,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    descriptor.descriptor_id,
                    descriptor.semantic_sha256,
                    descriptor.adapter_id,
                    descriptor.adapter_version,
                    descriptor.model_solver_role_id,
                    descriptor.acoustic_domain,
                    descriptor.model_dump_json(),
                    _utc_now(),
                ),
            )
        return descriptor

    def get_descriptor(
        self,
        descriptor_id: str,
    ) -> AcousticSolverAdapterDescriptor | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_solver_adapters
                WHERE descriptor_id=?
                """,
                (descriptor_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate_descriptor(
            AcousticSolverAdapterDescriptor.model_validate_json(
                row['payload_json']
            )
        )

    def _validate_dispatch(
        self,
        binding: AcousticSolverDispatchBinding,
    ) -> AcousticSolverDispatchBinding:
        binding = AcousticSolverDispatchBinding.model_validate(
            binding.model_dump(mode='python')
        )
        snapshot = self.snapshot_repository.get_snapshot(
            binding.acoustic_scene_snapshot_id
        )
        if snapshot is None:
            raise ValueError(
                'solver dispatch references missing AcousticSceneSnapshot'
            )
        if snapshot.semantic_sha256 != binding.acoustic_scene_snapshot_sha256:
            raise ValueError('solver dispatch AcousticSceneSnapshot hash mismatch')

        request = self.snapshot_repository.get_prediction_request(
            binding.prediction_request_id
        )
        if request is None:
            raise ValueError(
                'solver dispatch references missing AcousticPredictionRequest'
            )
        if (
            request.request_semantic_sha256
            != binding.prediction_request_semantic_sha256
            or request.deterministic_input_hash
            != binding.prediction_deterministic_input_hash
        ):
            raise ValueError('solver dispatch prediction request identity mismatch')
        if (
            request.acoustic_scene_snapshot_id != snapshot.snapshot_id
            or request.acoustic_scene_snapshot_sha256 != snapshot.semantic_sha256
        ):
            raise ValueError(
                'solver dispatch prediction request snapshot binding mismatch'
            )

        descriptor = self.get_descriptor(binding.adapter_descriptor_id)
        if descriptor is None:
            raise ValueError(
                'solver dispatch references missing adapter descriptor'
            )
        if descriptor.semantic_sha256 != binding.adapter_descriptor_semantic_sha256:
            raise ValueError('solver dispatch adapter descriptor hash mismatch')
        if descriptor.solver_implementation_ref != binding.solver_implementation_ref:
            raise ValueError('solver dispatch implementation authority mismatch')

        self._resolve_external_ref(
            binding.solver_configuration_ref,
            label='solver configuration',
        )
        policy = self._resolve_fidelity_policy(request)

        recomputed = bind_prediction_request_to_solver_adapter(
            snapshot=snapshot,
            request=request,
            adapter=descriptor,
            solver_configuration_ref=binding.solver_configuration_ref,
            numerical_fidelity_policy=policy,
        )
        if recomputed != binding:
            raise ValueError(
                'solver dispatch does not reproduce from exact authorities'
            )
        return binding

    def save_dispatch(
        self,
        binding: AcousticSolverDispatchBinding,
    ) -> AcousticSolverDispatchBinding:
        binding = self._validate_dispatch(binding)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_solver_dispatch_bindings
                WHERE binding_id=?
                """,
                (binding.binding_id,),
            ).fetchone()
            if existing is not None:
                persisted = AcousticSolverDispatchBinding.model_validate_json(
                    existing['payload_json']
                )
                if persisted != binding:
                    raise ValueError(
                        'solver dispatch id exists with different semantics'
                    )
                return self._validate_dispatch(persisted)
            connection.execute(
                """
                INSERT INTO cad_acoustic_solver_dispatch_bindings(
                    binding_id,
                    semantic_sha256,
                    prediction_request_id,
                    acoustic_scene_snapshot_id,
                    adapter_descriptor_id,
                    deterministic_solver_input_hash,
                    state,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding.binding_id,
                    binding.semantic_sha256,
                    binding.prediction_request_id,
                    binding.acoustic_scene_snapshot_id,
                    binding.adapter_descriptor_id,
                    binding.deterministic_solver_input_hash,
                    binding.state,
                    binding.model_dump_json(),
                    _utc_now(),
                ),
            )
        return binding

    def get_dispatch(
        self,
        binding_id: str,
    ) -> AcousticSolverDispatchBinding | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_solver_dispatch_bindings
                WHERE binding_id=?
                """,
                (binding_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate_dispatch(
            AcousticSolverDispatchBinding.model_validate_json(
                row['payload_json']
            )
        )
