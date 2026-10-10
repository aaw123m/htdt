
import sqlite3
from contextlib import (
    closing,
)
from pathlib import (
    Path,
)
from ...cad_schema import (
    connect_sqlite,
    ensure_native_schema,
    require_native_tables,
)
from ..domain.cad_acoustic_source_pose import (
    AcousticSourcePoseObservation,
)

class AcousticSourcePoseRepository:
    """Append-only acoustic-source-pose store on the shared cad DB."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        ensure_native_schema(self.path)
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection,
                'cad_acoustic_source_poses',
            )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_observation(
        self, observation: AcousticSourcePoseObservation
    ) -> AcousticSourcePoseObservation:
        """Append; identical re-save is a no-op, conflicting content fails."""
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_acoustic_source_poses('
                'observation_id, document_id, source_entity_id, verdict, '
                'observed_at_utc, semantic_sha256, payload_json) '
                'VALUES(?,?,?,?,?,?,?) '
                'ON CONFLICT(observation_id) DO NOTHING',
                (
                    observation.observation_id,
                    observation.document_id,
                    observation.source_entity_id,
                    observation.verdict,
                    observation.observed_at_utc,
                    observation.semantic_sha256,
                    observation.model_dump_json(),
                ),
            )
            row = connection.execute(
                'SELECT payload_json FROM cad_acoustic_source_poses '
                'WHERE observation_id=?',
                (observation.observation_id,),
            ).fetchone()
            if row['payload_json'] != observation.model_dump_json():
                raise ValueError(
                    f'acoustic source pose {observation.observation_id} '
                    'already persisted with different content — '
                    'observations are immutable'
                )
        return observation

    def get_observation(
        self, observation_id: str
    ) -> AcousticSourcePoseObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_acoustic_source_poses '
                'WHERE observation_id=?',
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        return AcousticSourcePoseObservation.model_validate_json(
            row['payload_json']
        )

    def list_observations_for_source(
        self, document_id: str, source_entity_id: str
    ) -> tuple[AcousticSourcePoseObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_acoustic_source_poses '
                'WHERE document_id=? AND source_entity_id=? '
                'ORDER BY observed_at_utc ASC, observation_id ASC',
                (document_id, source_entity_id),
            ).fetchall()
        return tuple(
            AcousticSourcePoseObservation.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

__all__ = [
    'AcousticSourcePoseRepository',
]
