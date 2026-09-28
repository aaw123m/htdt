"""Append-only persistence for auralization render specs and artifacts (#1202).

``AuralizationRenderSpec`` and ``AuralizationArtifact`` are immutable
authorities — their ids are sealed to their semantic digests by the model
validators, so persistence rows are insert-only and reads re-validate the
canonical payload (fail-closed on any drift). The rendered WAV bytes are
managed-asset content, not a payload column: they are installed
content-addressed under ``measurement-assets`` and every read re-verifies
the byte digest the artifact pinned in ``output_asset_sha256``.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256

from .cad_auralization import AuralizationArtifact, AuralizationRenderSpec
from .cad_schema import connect_sqlite, require_native_tables
from .managed_assets import MANAGED_ASSETS_DIRNAME, ManagedAssetStore


class CadAuralizationRepository:
    """Append-only storage for auralization specs and derived WAV artifacts."""

    def __init__(self, scene_repository) -> None:
        self.path = scene_repository.path
        self._asset_store = ManagedAssetStore(
            self.path.parent / MANAGED_ASSETS_DIRNAME
        )
        self._initialize()

    def _connect(self):
        return closing(connect_sqlite(self.path))

    def _initialize(self) -> None:
        with self._connect() as connection, connection:
            require_native_tables(
                connection,
                'cad_auralization_render_specs',
                'cad_auralization_artifacts',
            )

    # ------------------------------------------------------------------
    # Render specs
    # ------------------------------------------------------------------

    def save_render_spec(self, spec: AuralizationRenderSpec) -> None:
        if self.get_render_spec(spec.spec_id) is not None:
            raise ValueError('auralization render specs are append-only')
        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_auralization_render_specs (
                    spec_id, spec_semantic_sha256, document_id,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    spec.spec_id,
                    spec.semantic_sha256,
                    spec.document_id,
                    datetime.now(timezone.utc).isoformat(),
                    spec.model_dump_json(),
                ),
            )

    def get_render_spec(self, spec_id: str) -> AuralizationRenderSpec | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_auralization_render_specs '
                'WHERE spec_id=?',
                (spec_id,),
            ).fetchone()
        if row is None:
            return None
        return AuralizationRenderSpec.model_validate_json(row['payload_json'])

    def find_render_spec_by_sha(
        self, semantic_sha256: str
    ) -> AuralizationRenderSpec | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_auralization_render_specs '
                'WHERE spec_semantic_sha256=?',
                (semantic_sha256,),
            ).fetchone()
        if row is None:
            return None
        return AuralizationRenderSpec.model_validate_json(row['payload_json'])

    # ------------------------------------------------------------------
    # Derived artifacts (+ managed WAV bytes)
    # ------------------------------------------------------------------

    def save_artifact(
        self, artifact: AuralizationArtifact, wav_bytes: bytes
    ) -> None:
        if self.get_artifact(artifact.artifact_id) is not None:
            raise ValueError('auralization artifacts are append-only')
        if sha256(wav_bytes).hexdigest() != artifact.output_asset_sha256:
            raise ValueError(
                'auralization WAV bytes do not match artifact '
                'output_asset_sha256'
            )
        spec = self.get_render_spec(artifact.spec_id)
        if spec is None or spec.semantic_sha256 != artifact.spec_semantic_sha256:
            raise ValueError(
                'auralization artifact requires the exact persisted render spec'
            )
        self._asset_store.ensure_installed(
            artifact.output_asset_sha256, wav_bytes
        )
        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_auralization_artifacts (
                    artifact_id, artifact_semantic_sha256, spec_id,
                    output_asset_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.semantic_sha256,
                    artifact.spec_id,
                    artifact.output_asset_sha256,
                    datetime.now(timezone.utc).isoformat(),
                    artifact.model_dump_json(),
                ),
            )

    def get_artifact(self, artifact_id: str) -> AuralizationArtifact | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_auralization_artifacts '
                'WHERE artifact_id=?',
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        return AuralizationArtifact.model_validate_json(row['payload_json'])

    def find_artifact_by_sha(
        self, semantic_sha256: str
    ) -> AuralizationArtifact | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_auralization_artifacts '
                'WHERE artifact_semantic_sha256=?',
                (semantic_sha256,),
            ).fetchone()
        if row is None:
            return None
        return AuralizationArtifact.model_validate_json(row['payload_json'])

    def artifacts_for_spec(self, spec_id: str) -> tuple[AuralizationArtifact, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_auralization_artifacts '
                'WHERE spec_id=? ORDER BY artifact_id',
                (spec_id,),
            ).fetchall()
        return tuple(
            AuralizationArtifact.model_validate_json(row['payload_json'])
            for row in rows
        )

    def read_artifact_wav(self, artifact: AuralizationArtifact) -> bytes:
        """Return the artifact's WAV bytes, re-verified against their digest."""

        raw = self._asset_store.read_verified(artifact.output_asset_sha256)
        if raw is None:
            raise ValueError(
                'auralization artifact WAV is missing from the managed '
                f'asset store: {artifact.output_asset_sha256}'
            )
        return raw


__all__ = ['CadAuralizationRepository']
