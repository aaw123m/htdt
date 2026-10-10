
import sqlite3
from contextlib import (
    closing,
)
from pathlib import (
    Path,
)
from ..domain.cad_geometric_acoustics_response import (
    DeterministicPathFrequencyResponseArtifact,
)
from ...cad_repository import (
    SceneRepository,
)
from ...cad_schema import (
    connect_sqlite,
    ensure_native_schema,
    require_native_tables,
)
from ...canonical_json import (
    canonical_json as _canonical_json,
)
from ..domain.cad_hybrid_stitching import (
    CandidateInputResolver,
    ConventionAuthorityResolver,
    R150ResponseResolver,
    StitchedCompositionSpecResolver,
    StitchedHybridResponseArtifact,
    WaveArtifactPayloadResolver,
    WaveExcitationResolver,
    WaveResultResolver,
    compose_stitched_hybrid_response,
)
from ..domain.cad_hybrid_numerical_composition import (
    _complex_pressure_manifest,
    _excitation_ref,
    _response_ref,
)

class CadStitchedHybridResponseRepository:
    """Append-only stitched-response persistence with exact stale rejection."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        wave_result_resolver: WaveResultResolver,
        wave_artifact_payload_resolver: WaveArtifactPayloadResolver,
        candidate_input_resolver: CandidateInputResolver,
        wave_excitation_resolver: WaveExcitationResolver,
        r150_response_resolver: R150ResponseResolver,
        composition_spec_resolver: StitchedCompositionSpecResolver,
        convention_authority_resolver: ConventionAuthorityResolver,
    ) -> None:
        self.scene_repository = scene_repository
        self.path = Path(scene_repository.path)
        self.wave_result_resolver = wave_result_resolver
        self.wave_artifact_payload_resolver = wave_artifact_payload_resolver
        self.candidate_input_resolver = candidate_input_resolver
        self.wave_excitation_resolver = wave_excitation_resolver
        self.r150_response_resolver = r150_response_resolver
        self.composition_spec_resolver = composition_spec_resolver
        self.convention_authority_resolver = convention_authority_resolver
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'r160_stitched_hybrid_responses')

    def _rebuild(
        self,
        artifact: StitchedHybridResponseArtifact,
    ) -> StitchedHybridResponseArtifact:
        artifact = StitchedHybridResponseArtifact.model_validate(
            artifact.model_dump(mode='python')
        )
        spec = self.composition_spec_resolver(
            artifact.composition_spec.composition_spec_id
        )
        if spec is None or spec != artifact.composition_spec:
            raise ValueError('R160 stitched composition authority is missing/stale')
        normalization = self.convention_authority_resolver(
            spec.normalization_authority_ref
        )
        if (
            normalization is None
            or normalization.as_external_ref() != spec.normalization_authority_ref
        ):
            raise ValueError(
                'R160 stitched convention normalization authority is missing/stale'
            )

        result = self.wave_result_resolver(spec.r130_result.result_id)
        if (
            result is None
            or result.semantic_sha256 != spec.r130_result.semantic_sha256
        ):
            raise ValueError('R160 stitched exact R130 result is missing/stale')
        manifest = _complex_pressure_manifest(result)
        if manifest.artifact_authority != spec.r130_complex_pressure_artifact_ref:
            raise ValueError('R160 stitched exact R130 artifact changed')
        payload = self.wave_artifact_payload_resolver(
            spec.r130_complex_pressure_artifact_ref
        )

        candidate = self.candidate_input_resolver(
            spec.r130_candidate_input.execution_input_id
        )
        if (
            candidate is None
            or candidate.semantic_sha256 != spec.r130_candidate_input.semantic_sha256
            or candidate.authority_version
            != spec.r130_candidate_input.authority_version
        ):
            raise ValueError('R160 stitched candidate input is missing/stale')

        excitation = self.wave_excitation_resolver(
            spec.wave_excitation_ref.authority_id
        )
        if (
            excitation is None
            or _excitation_ref(excitation) != spec.wave_excitation_ref
        ):
            raise ValueError('R160 stitched wave excitation is missing/stale')

        responses: list[DeterministicPathFrequencyResponseArtifact] = []
        for ref in spec.r150_response_refs:
            response = self.r150_response_resolver(ref.authority_id)
            if response is None or _response_ref(response) != ref:
                raise ValueError(
                    f'R160 stitched R150 response is missing/stale: '
                    f'{ref.authority_id}'
                )
            responses.append(response)

        rebuilt = compose_stitched_hybrid_response(
            spec=spec,
            r130_result=result,
            r130_artifact_payload=payload,
            r130_candidate_input=candidate,
            wave_excitation=excitation,
            r150_responses=responses,
            normalization_authority=normalization,
        )
        if rebuilt != artifact:
            raise ValueError(
                'R160 persisted stitched response no longer reproduces exactly'
            )
        return rebuilt

    def save(self, artifact: StitchedHybridResponseArtifact) -> None:
        artifact = self._rebuild(artifact)
        payload = _canonical_json(artifact.model_dump(mode='json'))
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT semantic_sha256, payload_json '
                'FROM r160_stitched_hybrid_responses WHERE artifact_id=?',
                (artifact.artifact_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing['semantic_sha256'] != artifact.semantic_sha256
                    or existing['payload_json'] != payload
                ):
                    raise ValueError('R160 stitched artifact identity collision')
                return
            connection.execute(
                'INSERT INTO r160_stitched_hybrid_responses '
                '(artifact_id, semantic_sha256, composition_spec_id, payload_json) '
                'VALUES (?, ?, ?, ?)',
                (
                    artifact.artifact_id,
                    artifact.semantic_sha256,
                    artifact.composition_spec.composition_spec_id,
                    payload,
                ),
            )

    def get(
        self,
        artifact_id: str,
    ) -> StitchedHybridResponseArtifact | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM r160_stitched_hybrid_responses '
                'WHERE artifact_id=?',
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        artifact = StitchedHybridResponseArtifact.model_validate_json(
            row['payload_json']
        )
        return self._rebuild(artifact)

__all__ = [
    'CadStitchedHybridResponseRepository',
]
