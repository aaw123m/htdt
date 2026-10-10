
import sqlite3
from contextlib import (
    closing,
)
from pathlib import (
    Path,
)
from ..domain.cad_acoustic_snapshot import (
    AcousticPredictionRequest,
)
from ..domain.cad_acoustic_solver_result import (
    AcousticSolverResultEnvelope,
)
from ...cad_repository import (
    SceneRepository,
)
from ...cad_schema import (
    connect_sqlite,
    ensure_native_schema,
    require_native_tables,
)
from ...clock import (
    utc_now_iso as _utc_now,
)
from ..domain.cad_hybrid_acoustic_result import (
    DeterministicPathResolver,
    ExternalPayloadResolver,
    HybridAcousticResult,
    HybridCompositionSpec,
    HybridSolverResultRef,
    HybridStitchingPolicy,
    SnapshotRequestResolver,
    SolverResultResolver,
    _environment_identity,
    _receiver_identities,
    _request_ref,
    _result_ref,
    _source_identities,
    build_hybrid_acoustic_result,
    compose_hybrid_acoustic_result,
)
from ..domain.cad_geometric_acoustics_contracts import (
    DeterministicPathArtifact,
)

class CadHybridAcousticResultRepository:
    """Append-only R160 persistence with exact source re-resolution."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        snapshot_request_resolver: SnapshotRequestResolver,
        solver_result_resolver: SolverResultResolver,
        deterministic_path_resolver: DeterministicPathResolver,
        external_payload_resolver: ExternalPayloadResolver,
    ) -> None:
        self.scene_repository = scene_repository
        self.snapshot_request_resolver = snapshot_request_resolver
        self.solver_result_resolver = solver_result_resolver
        self.deterministic_path_resolver = deterministic_path_resolver
        self.external_payload_resolver = external_payload_resolver
        self.path = Path(scene_repository.path)
        for label, resolver in (
            ('snapshot/request', snapshot_request_resolver),
            ('solver result', solver_result_resolver),
            ('deterministic path', deterministic_path_resolver),
        ):
            if Path(resolver.path) != self.path:
                raise ValueError(
                    f'R160 hybrid and {label} repositories must share one '
                    'native CAD database'
                )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_hybrid_stitching_policies', 'cad_hybrid_acoustic_results')

    def get_policy(
        self,
        policy_id: str,
    ) -> HybridStitchingPolicy | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_hybrid_stitching_policies
                WHERE policy_id=?
                """,
                (policy_id,),
            ).fetchone()
        if row is None:
            return None
        return HybridStitchingPolicy.model_validate_json(row['payload_json'])

    def _resolve_result(
        self,
        ref: HybridSolverResultRef,
    ) -> AcousticSolverResultEnvelope:
        result = self.solver_result_resolver.get(ref.result_id)
        if result is None:
            raise ValueError(
                'R160 references missing/stale AcousticSolverResultEnvelope'
            )
        if _result_ref(result) != ref:
            raise ValueError(
                'R160 solver result exact identity/provenance mismatch'
            )
        return result

    def _validate(
        self,
        hybrid: HybridAcousticResult,
        *,
        policy: HybridStitchingPolicy | None = None,
    ) -> HybridAcousticResult:
        hybrid = HybridAcousticResult.model_validate(
            hybrid.model_dump(mode='python')
        )
        snapshot = self.snapshot_request_resolver.get_snapshot(
            hybrid.acoustic_scene_snapshot_id
        )
        if (
            snapshot is None
            or snapshot.semantic_sha256
            != hybrid.acoustic_scene_snapshot_sha256
            or snapshot.scene_revision_id != hybrid.scene_revision_id
            or snapshot.scene_content_hash != hybrid.scene_content_hash
            or snapshot.semantic_geometry_id != hybrid.semantic_geometry_id
            or snapshot.semantic_geometry_sha256
            != hybrid.semantic_geometry_sha256
            or _source_identities(snapshot) != hybrid.source_identities
            or _receiver_identities(snapshot) != hybrid.receiver_identity_order
            or _environment_identity(snapshot) != hybrid.environment_identity
        ):
            raise ValueError(
                'R160 exact snapshot/geometry/source/receiver/environment '
                'compatibility failed'
            )

        requests: list[AcousticPredictionRequest] = []
        for ref in hybrid.prediction_requests:
            request = self.snapshot_request_resolver.get_prediction_request(
                ref.request_id
            )
            if request is None or _request_ref(request) != ref:
                raise ValueError(
                    'R160 references missing/stale AcousticPredictionRequest'
                )
            if (
                request.acoustic_scene_snapshot_id != snapshot.snapshot_id
                or request.acoustic_scene_snapshot_sha256
                != snapshot.semantic_sha256
            ):
                raise ValueError(
                    'R160 request no longer resolves to exact snapshot'
                )
            requests.append(request)

        results = [
            self._resolve_result(ref)
            for ref in hybrid.participating_solver_results
        ]

        if policy is None:
            policy = self.get_policy(hybrid.stitching_policy_ref.authority_id)
        if policy is None or policy.as_external_ref() != hybrid.stitching_policy_ref:
            raise ValueError(
                'R160 references missing/mismatched stitching policy authority'
            )

        path_artifacts: list[DeterministicPathArtifact] = []
        if hybrid.deterministic_path_set is not None:
            path_ref = hybrid.deterministic_path_set.artifact_authority
            path = self.deterministic_path_resolver.get(path_ref.authority_id)
            if path is None or path.as_external_ref() != path_ref:
                raise ValueError(
                    'R160 references missing/stale R150 DeterministicPathArtifact'
                )
            path_artifacts.append(path)

        regenerated = build_hybrid_acoustic_result(
            snapshot=snapshot,
            prediction_requests=requests,
            solver_results=results,
            stitching_policy=policy,
            deterministic_path_artifacts=path_artifacts,
            external_payload_resolver=self.external_payload_resolver,
            late_energy_decay_state=(
                'NOT_PROVIDED'
                if hybrid.late_energy_decay.state == 'AVAILABLE'
                else hybrid.late_energy_decay.state
            ),
            late_energy_decay_reason=hybrid.late_energy_decay.reason,
        )
        if hybrid.composition_spec is not None:
            if len(path_artifacts) != 1:
                raise ValueError(
                    'R160 bounded composition requires exact persisted R150 artifact'
                )
            regenerated = compose_hybrid_acoustic_result(
                hybrid=regenerated,
                composition_spec=hybrid.composition_spec,
                deterministic_path_artifact=path_artifacts[0],
            )
        if regenerated != hybrid:
            raise ValueError(
                'R160 hybrid result does not reproduce from exact persisted '
                'authorities'
            )
        return hybrid

    def save(
        self,
        hybrid: HybridAcousticResult,
        *,
        policy: HybridStitchingPolicy,
    ) -> HybridAcousticResult:
        policy = HybridStitchingPolicy.model_validate(
            policy.model_dump(mode='python')
        )
        if policy.as_external_ref() != hybrid.stitching_policy_ref:
            raise ValueError(
                'R160 save policy does not match hybrid policy identity'
            )
        hybrid = self._validate(hybrid, policy=policy)

        with closing(self._connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                existing_policy = connection.execute(
                    """
                    SELECT payload_json
                    FROM cad_hybrid_stitching_policies
                    WHERE policy_id=?
                    """,
                    (policy.policy_id,),
                ).fetchone()
                if existing_policy is not None:
                    persisted_policy = HybridStitchingPolicy.model_validate_json(
                        existing_policy['payload_json']
                    )
                    if persisted_policy != policy:
                        raise ValueError(
                            'hybrid stitching policy id exists with '
                            'different semantics'
                        )
                else:
                    connection.execute(
                        """
                        INSERT INTO cad_hybrid_stitching_policies(
                            policy_id,
                            semantic_sha256,
                            mode,
                            payload_json,
                            recorded_at_utc
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            policy.policy_id,
                            policy.semantic_sha256,
                            policy.mode,
                            policy.model_dump_json(),
                            _utc_now(),
                        ),
                    )

                existing = connection.execute(
                    """
                    SELECT payload_json
                    FROM cad_hybrid_acoustic_results
                    WHERE hybrid_result_id=?
                    """,
                    (hybrid.hybrid_result_id,),
                ).fetchone()
                if existing is not None:
                    persisted = HybridAcousticResult.model_validate_json(
                        existing['payload_json']
                    )
                    if persisted != hybrid:
                        raise ValueError(
                            'HybridAcousticResult id exists with '
                            'different semantics'
                        )
                else:
                    connection.execute(
                        """
                        INSERT INTO cad_hybrid_acoustic_results(
                            hybrid_result_id,
                            semantic_sha256,
                            acoustic_scene_snapshot_id,
                            scene_revision_id,
                            stitching_policy_id,
                            payload_json,
                            recorded_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            hybrid.hybrid_result_id,
                            hybrid.semantic_sha256,
                            hybrid.acoustic_scene_snapshot_id,
                            hybrid.scene_revision_id,
                            policy.policy_id,
                            hybrid.model_dump_json(),
                            _utc_now(),
                        ),
                    )
                connection.commit()
            except Exception:  # error-boundary: rollback before re-raise — any commit failure rolls the transaction back so a partial hybrid record never persists (noqa: BLE001)
                connection.rollback()
                raise
        return hybrid

    def get(
        self,
        hybrid_result_id: str,
        *,
        expected_composition_spec: HybridCompositionSpec | None = None,
    ) -> HybridAcousticResult | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_hybrid_acoustic_results
                WHERE hybrid_result_id=?
                """,
                (hybrid_result_id,),
            ).fetchone()
        if row is None:
            return None
        hybrid = self._validate(
            HybridAcousticResult.model_validate_json(row['payload_json'])
        )
        if expected_composition_spec is not None:
            if (
                hybrid.composition_spec is None
                or hybrid.composition_spec != expected_composition_spec
            ):
                raise ValueError(
                    'R160 persisted hybrid composition is stale for expected spec'
                )
        return hybrid

__all__ = [
    'CadHybridAcousticResultRepository',
]
