
import sqlite3
from contextlib import (
    closing,
)
from pathlib import (
    Path,
)
from ...cad_directivity import (
    DirectivityDataset,
)
from ...cad_repository import (
    SceneRepository,
)
from ...cad_schema import (
    connect_sqlite,
    require_native_tables,
)
from ...canonical_json import (
    canonical_json as _canonical_json,
)
from ...r120_geometry_compiler import (
    ExactExternalAuthorityRef,
)
from ..domain.cad_geometric_acoustics_response import (
    AcousticEnvironmentAuthority,
    DeterministicPathFrequencyResponseArtifact,
    FrequencyGridAuthority,
    PathArtifactResolver,
    PathResponseConfiguration,
    PointSourceNormalizationAuthority,
    PortalAcousticTransferAuthority,
    ReceiverResponseAuthority,
    ResponseDependency,
    ResponseDependencyResolver,
    SourceResponseAuthority,
    SurfaceReflectionTransferAuthority,
    _dependency_ref,
    _ref_payload,
    build_deterministic_path_frequency_response,
)
from ..domain.cad_geometric_acoustics_contracts import (
    DeterministicGaExecutionInput,
)

class CadPathFrequencyResponseRepository:
    """Append-only per-path response persistence with exact dependency re-resolution."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        path_artifact_resolver: PathArtifactResolver,
        dependency_resolver: ResponseDependencyResolver,
    ) -> None:
        self.scene_repository = scene_repository
        self.path = Path(scene_repository.path)
        self.path_artifact_resolver = path_artifact_resolver
        self.dependency_resolver = dependency_resolver
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'r150_path_frequency_response_artifacts')

    def _resolve_dependency(
        self,
        ref: ExactExternalAuthorityRef,
    ) -> ResponseDependency:
        value = self.dependency_resolver(ref)
        if value is None:
            raise ValueError(
                f'path response dependency is missing/stale: {ref.authority_id}'
            )
        if _dependency_ref(value) != ref:
            raise ValueError(
                f'path response dependency exact identity mismatch: {ref.authority_id}'
            )
        return value

    def _rebuild(
        self,
        artifact: DeterministicPathFrequencyResponseArtifact,
    ) -> DeterministicPathFrequencyResponseArtifact:
        path_artifact = self.path_artifact_resolver(
            artifact.deterministic_path_artifact_id
        )
        if path_artifact is None:
            raise ValueError('deterministic path artifact is missing/stale')
        if path_artifact.semantic_sha256 != artifact.deterministic_path_artifact_sha256:
            raise ValueError('deterministic path artifact exact identity mismatch')
        path = next(
            (
                item
                for item in path_artifact.paths
                if item.path_id == artifact.deterministic_path_id
            ),
            None,
        )
        if path is None or path.semantic_sha256 != artifact.deterministic_path_sha256:
            raise ValueError('deterministic path exact identity mismatch')

        resolved = {
            _ref_payload(ref): self._resolve_dependency(ref)
            for ref in artifact.dependency_refs
        }

        def typed(cls):
            matches = [value for value in resolved.values() if isinstance(value, cls)]
            if len(matches) != 1:
                raise ValueError(
                    f'path response requires exactly one resolved {cls.__name__} authority'
                )
            return matches[0]

        execution_input = typed(DeterministicGaExecutionInput)
        source = typed(SourceResponseAuthority)
        receiver = typed(ReceiverResponseAuthority)
        environment = typed(AcousticEnvironmentAuthority)
        grid = typed(FrequencyGridAuthority)
        configuration = typed(PathResponseConfiguration)

        normalizations = [
            value
            for value in resolved.values()
            if isinstance(value, PointSourceNormalizationAuthority)
        ]
        normalization = normalizations[0] if len(normalizations) == 1 else None
        if len(normalizations) > 1:
            raise ValueError('multiple point-source normalization authorities resolved')

        datasets = [
            value for value in resolved.values() if isinstance(value, DirectivityDataset)
        ]
        dataset = datasets[0] if len(datasets) == 1 else None
        if len(datasets) > 1:
            raise ValueError('multiple directivity datasets resolved for one source')

        reflections = {
            value.source_surface_id: value
            for value in resolved.values()
            if isinstance(value, SurfaceReflectionTransferAuthority)
        }
        portals = {
            (value.portal_id, value.from_region_id, value.to_region_id): value
            for value in resolved.values()
            if isinstance(value, PortalAcousticTransferAuthority)
        }

        r120_ref = next(
            (
                ref
                for ref in artifact.dependency_refs
                if ref.authority_id == path_artifact.r120_compiled_geometry_id
                and ref.semantic_hash_sha256
                == path_artifact.r120_compiled_geometry_sha256
            ),
            None,
        )
        if r120_ref is None:
            raise ValueError('R120 compiled geometry dependency is missing/stale')

        portal_interactions = tuple(
            item
            for item in (path.ordered_interactions or ())
            if item.kind == 'portal_crossing'
        )
        portal_ref: ExactExternalAuthorityRef | None = None
        if portal_interactions:
            portal_transfer_values = tuple(portals.values())
            if portal_transfer_values:
                candidate = portal_transfer_values[0].portal_geometry_authority_ref
                if any(
                    item.portal_geometry_authority_ref != candidate
                    for item in portal_transfer_values
                ):
                    raise ValueError('Portal transfer authorities bind different Portal geometry')
                portal_ref = candidate
            else:
                portal_refs = [
                    ref
                    for ref in artifact.dependency_refs
                    if ref.authority_id.startswith('r120-portals:')
                ]
                portal_ref = portal_refs[0] if len(portal_refs) == 1 else None

        rebuilt = build_deterministic_path_frequency_response(
            path_artifact=path_artifact,
            execution_input=execution_input,
            path_id=artifact.deterministic_path_id,
            r120_geometry_ref=r120_ref,
            source_authority=source,
            point_source_normalization=normalization,
            receiver_authority=receiver,
            environment=environment,
            frequency_grid=grid,
            configuration=configuration,
            surface_reflections=reflections,
            portal_geometry_authority_ref=portal_ref,
            portal_transfers=portals,
            directivity_dataset=dataset,
        )
        if rebuilt != artifact:
            raise ValueError(
                'persisted path response no longer reproduces from current exact authorities'
            )
        return rebuilt

    def save(self, artifact: DeterministicPathFrequencyResponseArtifact) -> None:
        self._rebuild(artifact)
        payload = _canonical_json(artifact.model_dump(mode='json'))
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT semantic_sha256, payload_json '
                'FROM r150_path_frequency_response_artifacts WHERE artifact_id = ?',
                (artifact.artifact_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing['semantic_sha256'] != artifact.semantic_sha256
                    or existing['payload_json'] != payload
                ):
                    raise ValueError('immutable path response artifact identity collision')
                return
            connection.execute(
                'INSERT INTO r150_path_frequency_response_artifacts '
                '(artifact_id, semantic_sha256, payload_json) VALUES (?, ?, ?)',
                (artifact.artifact_id, artifact.semantic_sha256, payload),
            )

    def get(
        self,
        artifact_id: str,
    ) -> DeterministicPathFrequencyResponseArtifact | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM r150_path_frequency_response_artifacts '
                'WHERE artifact_id = ?',
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        artifact = DeterministicPathFrequencyResponseArtifact.model_validate_json(
            row['payload_json']
        )
        return self._rebuild(artifact)

__all__ = [
    'CadPathFrequencyResponseRepository',
]
