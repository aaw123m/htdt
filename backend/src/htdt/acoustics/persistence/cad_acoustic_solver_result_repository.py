
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
from ..domain.cad_acoustic_solver_adapter import (
    AcousticSolverDispatchBinding,
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
from ...r120_geometry_compiler import (
    ExactExternalAuthorityRef,
)
from ..domain.cad_acoustic_solver_result import (
    AcousticDispatchResolver,
    AcousticPredictionRequestResolver,
    AcousticSolverArtifactManifest,
    AcousticSolverArtifactManifestResolver,
    AcousticSolverResultEnvelope,
    ExternalAuthorityResolver,
    _domain_contains,
    _resolve_artifact_manifest,
    build_acoustic_solver_result_envelope,
)

class CadAcousticSolverResultRepository:
    """Append-only exact arbitrary-room solver-result persistence.

    Every artifact binding is re-resolved against the typed
    ``artifact_manifest_resolver`` on save and on read; the declared
    observable, encoding schema and valid frequency domain must reproduce the
    resolved :class:`AcousticSolverArtifactManifest` exactly.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        dispatch_resolver: AcousticDispatchResolver,
        request_resolver: AcousticPredictionRequestResolver,
        external_authority_resolver: ExternalAuthorityResolver,
        artifact_manifest_resolver: AcousticSolverArtifactManifestResolver,
    ) -> None:
        self.scene_repository = scene_repository
        self.dispatch_resolver = dispatch_resolver
        self.request_resolver = request_resolver
        self.external_authority_resolver = external_authority_resolver
        self.artifact_manifest_resolver = artifact_manifest_resolver
        self.path = Path(scene_repository.path)
        for label, resolver in (
            ('solver dispatch', dispatch_resolver),
            ('prediction request', request_resolver),
        ):
            if Path(resolver.path) != self.path:
                raise ValueError(
                    f'acoustic solver result and {label} repositories must '
                    'share one native CAD database'
                )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_acoustic_solver_results')

    def _resolve_external(
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

    def _validate(
        self,
        result: AcousticSolverResultEnvelope,
    ) -> AcousticSolverResultEnvelope:
        result = AcousticSolverResultEnvelope.model_validate(
            result.model_dump(mode='python')
        )
        dispatch = self.dispatch_resolver.get_dispatch(
            result.dispatch_binding_id
        )
        if dispatch is None:
            raise ValueError(
                'solver result references missing AcousticSolverDispatchBinding'
            )
        if dispatch.semantic_sha256 != result.dispatch_binding_sha256:
            raise ValueError('solver result dispatch binding hash mismatch')

        request = self.request_resolver.get_prediction_request(
            result.prediction_request_id
        )
        if request is None:
            raise ValueError(
                'solver result references missing AcousticPredictionRequest'
            )

        for ref, label in (
            (result.execution_provenance_ref, 'execution provenance'),
            (result.solver_implementation_ref, 'solver implementation'),
            (result.solver_configuration_ref, 'solver configuration'),
        ):
            self._resolve_external(ref, label=label)
        for item in result.artifacts:
            self._resolve_external(
                item.artifact_authority,
                label=f'{item.observable} artifact',
            )
            self._resolve_external(
                item.encoding_schema_ref,
                label=f'{item.observable} encoding schema',
            )
            manifest = _resolve_artifact_manifest(
                self.artifact_manifest_resolver,
                item,
            )
            if not _domain_contains(
                manifest.valid_frequency_domain,
                request.requested_frequency_domain,
            ):
                raise ValueError(
                    'solver result artifact manifest does not cover requested '
                    f'frequency domain: {item.observable}'
                )

        regenerated = build_acoustic_solver_result_envelope(
            dispatch=dispatch,
            request=request,
            execution_id=result.execution_id,
            execution_provenance_ref=result.execution_provenance_ref,
            artifacts=result.artifacts,
            completed_at_utc=result.completed_at_utc,
        )
        if regenerated != result:
            raise ValueError(
                'solver result does not reproduce from exact persisted authorities'
            )
        return result

    def save(
        self,
        result: AcousticSolverResultEnvelope,
    ) -> AcousticSolverResultEnvelope:
        result = self._validate(result)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_solver_results
                WHERE result_id=?
                """,
                (result.result_id,),
            ).fetchone()
            if existing is not None:
                persisted = AcousticSolverResultEnvelope.model_validate_json(
                    existing['payload_json']
                )
                if persisted != result:
                    raise ValueError(
                        'AcousticSolverResultEnvelope id exists with '
                        'different semantics'
                    )
                return self._validate(persisted)
            connection.execute(
                """
                INSERT INTO cad_acoustic_solver_results(
                    result_id,
                    semantic_sha256,
                    execution_id,
                    dispatch_binding_id,
                    prediction_request_id,
                    acoustic_scene_snapshot_id,
                    deterministic_solver_input_hash,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.result_id,
                    result.semantic_sha256,
                    result.execution_id,
                    result.dispatch_binding_id,
                    result.prediction_request_id,
                    result.acoustic_scene_snapshot_id,
                    result.deterministic_solver_input_hash,
                    result.model_dump_json(),
                    _utc_now(),
                ),
            )
        return result

    def get(
        self,
        result_id: str,
    ) -> AcousticSolverResultEnvelope | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_solver_results
                WHERE result_id=?
                """,
                (result_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate(
            AcousticSolverResultEnvelope.model_validate_json(
                row['payload_json']
            )
        )

__all__ = [
    'CadAcousticSolverResultRepository',
]
