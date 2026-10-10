
import sqlite3
from contextlib import (
    closing,
)
from pathlib import (
    Path,
)
from ..domain.cad_surface_scattering import (
    SurfaceScatteringEvidence,
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
from ..domain.cad_hybrid_late_energy import (
    LateEnergyDecayArtifact,
    PathArtifactResolver,
    ScatteringEvidenceResolver,
    solve_late_energy_decay,
    surface_scattering_evidence_ref,
)

class CadLateEnergyDecayRepository:
    """Append-only late-energy persistence with exact stale rejection."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        path_artifact_resolver: PathArtifactResolver,
        scattering_evidence_resolver: ScatteringEvidenceResolver,
    ) -> None:
        self.scene_repository = scene_repository
        self.path = Path(scene_repository.path)
        self.path_artifact_resolver = path_artifact_resolver
        self.scattering_evidence_resolver = scattering_evidence_resolver
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'r160_late_energy_decay_artifacts')

    def _rebuild(
        self,
        artifact: LateEnergyDecayArtifact,
    ) -> LateEnergyDecayArtifact:
        artifact = LateEnergyDecayArtifact.model_validate(
            artifact.model_dump(mode='python')
        )
        path_artifact = self.path_artifact_resolver(
            artifact.deterministic_path_artifact_ref.authority_id
        )
        if (
            path_artifact is None
            or path_artifact.as_external_ref()
            != artifact.deterministic_path_artifact_ref
        ):
            raise ValueError(
                'R160 late-energy deterministic path artifact is missing/stale'
            )
        evidences: dict[str, SurfaceScatteringEvidence] = {}
        for capability in artifact.late_field_input.surface_capabilities:
            if capability.evidence_ref is None:
                continue
            evidence = self.scattering_evidence_resolver(
                capability.evidence_ref.authority_id
            )
            if evidence is None or (
                surface_scattering_evidence_ref(evidence)
                != capability.evidence_ref
            ):
                raise ValueError(
                    'R160 late-energy scattering evidence is missing/stale'
                )
            evidences[capability.evidence_ref.authority_id] = evidence
        rebuilt = solve_late_energy_decay(
            path_artifact=path_artifact,
            late_field_input=artifact.late_field_input,
            decay_law=artifact.decay_law,
            scattering_evidence=evidences,
        )
        if rebuilt != artifact:
            raise ValueError(
                'R160 persisted late-energy artifact no longer reproduces exactly'
            )
        return rebuilt

    def save(self, artifact: LateEnergyDecayArtifact) -> LateEnergyDecayArtifact:
        artifact = self._rebuild(artifact)
        payload = _canonical_json(artifact.model_dump(mode='json'))
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT semantic_sha256, payload_json '
                'FROM r160_late_energy_decay_artifacts WHERE artifact_id=?',
                (artifact.artifact_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing['semantic_sha256'] != artifact.semantic_sha256
                    or existing['payload_json'] != payload
                ):
                    raise ValueError(
                        'R160 late-energy artifact identity collision'
                    )
                return artifact
            connection.execute(
                'INSERT INTO r160_late_energy_decay_artifacts '
                '(artifact_id, semantic_sha256, late_field_input_id, '
                'payload_json) VALUES (?, ?, ?, ?)',
                (
                    artifact.artifact_id,
                    artifact.semantic_sha256,
                    artifact.late_field_input.input_id,
                    payload,
                ),
            )
        return artifact

    def get(self, artifact_id: str) -> LateEnergyDecayArtifact | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM r160_late_energy_decay_artifacts '
                'WHERE artifact_id=?',
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        return self._rebuild(
            LateEnergyDecayArtifact.model_validate_json(row['payload_json'])
        )

__all__ = [
    'CadLateEnergyDecayRepository',
]
