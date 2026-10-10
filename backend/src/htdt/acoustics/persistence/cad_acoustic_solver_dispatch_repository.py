
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
import sqlite3

from ..domain.cad_acoustic_snapshot import AcousticPredictionRequest
from .cad_acoustic_snapshot_repository import (
    AcousticSnapshotAuthorityResolvers,
    CadAcousticSnapshotRepository,
)
from ..domain.cad_acoustic_solver_adapter import (
    AcousticNumericalFidelityPolicy,
    AcousticSolverAdapterDescriptor,
    AcousticSolverDispatchBinding,
    NumericalFidelityPolicyResolver,
    bind_prediction_request_to_solver_adapter,
)
from ...cad_repository import SceneRepository
from ...cad_schema import (
    ensure_native_schema,
    require_native_tables,
    connect_sqlite,
)
from ..domain.cad_solver_capability_manifest import (
    SolverCapabilityManifest,
    build_solver_capability_manifest,
    derive_solver_capability_rows,
)
from ...r120_geometry_compiler import ExactExternalAuthorityRef
from ...clock import utc_now_iso as _utc_now

ExternalAuthorityResolver = Callable[
    [ExactExternalAuthorityRef],
    ExactExternalAuthorityRef | None,
]

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
            require_native_tables(connection, 'cad_acoustic_solver_adapters', 'cad_acoustic_solver_dispatch_bindings', 'cad_solver_capability_manifests')

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
                descriptor = persisted
            else:
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
        self._emit_declared_capability_manifest(descriptor)
        return descriptor

    def _emit_declared_capability_manifest(
        self,
        descriptor: AcousticSolverAdapterDescriptor,
    ) -> SolverCapabilityManifest:
        """Persist the capability manifest a persisted descriptor declares.

        Descriptor persistence is the declaration emit point for every
        solver lane: the derivation table marks unevidenced phenomena
        UNSUPPORTED (fail closed), so an identical manifest re-save is a
        deduped no-op.
        """
        manifest = build_solver_capability_manifest(
            descriptor=descriptor,
            rows=derive_solver_capability_rows(descriptor),
        )
        return self.save_capability_manifest(manifest)

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
        *,
        _validated_snapshot=None,
        _validated_request=None,
    ) -> AcousticSolverDispatchBinding:
        binding = AcousticSolverDispatchBinding.model_validate(
            binding.model_dump(mode='python')
        )
        if (
            _validated_snapshot is not None
            and _validated_snapshot.snapshot_id
            == binding.acoustic_scene_snapshot_id
        ):
            snapshot = _validated_snapshot
        else:
            snapshot = self.snapshot_repository.get_snapshot(
                binding.acoustic_scene_snapshot_id
            )
        if snapshot is None:
            raise ValueError(
                'solver dispatch references missing AcousticSceneSnapshot'
            )
        if snapshot.semantic_sha256 != binding.acoustic_scene_snapshot_sha256:
            raise ValueError('solver dispatch AcousticSceneSnapshot hash mismatch')

        if (
            _validated_request is not None
            and _validated_request.request_id == binding.prediction_request_id
        ):
            request = _validated_request
        else:
            request = self.snapshot_repository.get_prediction_request(
                binding.prediction_request_id,
                _validated_snapshot=snapshot,
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
                return persisted
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
        *,
        _validated_snapshot=None,
        _validated_request=None,
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
            ),
            _validated_snapshot=_validated_snapshot,
            _validated_request=_validated_request,
        )

    def _validate_manifest(
        self,
        manifest: SolverCapabilityManifest,
    ) -> SolverCapabilityManifest:
        """Fail closed unless the manifest binds a persisted exact descriptor."""
        manifest = SolverCapabilityManifest.model_validate(
            manifest.model_dump(mode='python')
        )
        descriptor = self.get_descriptor(manifest.adapter_descriptor_id)
        if descriptor is None:
            raise ValueError(
                'capability manifest references a solver adapter descriptor '
                'that is not persisted'
            )
        checks = (
            (
                descriptor.semantic_sha256,
                manifest.adapter_descriptor_semantic_sha256,
                'descriptor semantic hash',
            ),
            (descriptor.adapter_id, manifest.adapter_id, 'adapter id'),
            (
                descriptor.adapter_version,
                manifest.adapter_version,
                'adapter version',
            ),
            (
                descriptor.model_solver_role_id,
                manifest.model_solver_role_id,
                'model solver role id',
            ),
            (
                descriptor.acoustic_domain,
                manifest.acoustic_domain,
                'acoustic domain',
            ),
            (
                descriptor.valid_frequency_domain,
                manifest.solver_valid_frequency_domain,
                'solver valid frequency domain',
            ),
        )
        for expected, actual, label in checks:
            if expected != actual:
                raise ValueError(f'capability manifest {label} mismatch')
        return manifest

    def save_capability_manifest(
        self,
        manifest: SolverCapabilityManifest,
    ) -> SolverCapabilityManifest:
        manifest = self._validate_manifest(manifest)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_solver_capability_manifests
                WHERE manifest_id=?
                """,
                (manifest.manifest_id,),
            ).fetchone()
            if existing is not None:
                persisted = SolverCapabilityManifest.model_validate_json(
                    existing['payload_json']
                )
                if persisted != manifest:
                    raise ValueError(
                        'capability manifest id exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_solver_capability_manifests(
                    manifest_id,
                    semantic_sha256,
                    adapter_descriptor_id,
                    adapter_id,
                    acoustic_domain,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.manifest_id,
                    manifest.semantic_sha256,
                    manifest.adapter_descriptor_id,
                    manifest.adapter_id,
                    manifest.acoustic_domain,
                    manifest.model_dump_json(),
                    _utc_now(),
                ),
            )
        return manifest

    def get_capability_manifest(
        self,
        manifest_id: str,
    ) -> SolverCapabilityManifest | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_solver_capability_manifests
                WHERE manifest_id=?
                """,
                (manifest_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate_manifest(
            SolverCapabilityManifest.model_validate_json(row['payload_json'])
        )

    def list_capability_manifests(
        self,
        *,
        adapter_descriptor_id: str | None = None,
    ) -> tuple[SolverCapabilityManifest, ...]:
        query = (
            'SELECT payload_json FROM cad_solver_capability_manifests'
        )
        params: tuple[str, ...] = ()
        if adapter_descriptor_id is not None:
            query += ' WHERE adapter_descriptor_id=?'
            params = (adapter_descriptor_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            self._validate_manifest(
                SolverCapabilityManifest.model_validate_json(
                    row['payload_json']
                )
            )
            for row in rows
        )
