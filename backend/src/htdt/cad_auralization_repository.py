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
from .cad_auralization_review import (
    AuralizationCapability,
    AuralizationReviewPackage,
    AuralizationRoutingDeclaration,
    MeasuredPredictedListeningValidation,
)
from .cad_schema import connect_sqlite, require_native_tables
from .managed_assets import MANAGED_ASSETS_DIRNAME, ManagedAssetStore


class CadAuralizationRepository:
    """Append-only storage for auralization specs, derived WAV artifacts,
    capability/routing authorities, review packages and listening
    validations (#538)."""

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
                'cad_auralization_capabilities',
                'cad_auralization_routing_declarations',
                'cad_auralization_review_packages',
                'cad_auralization_listening_validations',
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

    # ------------------------------------------------------------------
    # Routing declarations (#538)
    # ------------------------------------------------------------------

    def save_routing(self, routing: AuralizationRoutingDeclaration) -> None:
        if self.get_routing(routing.routing_id) is not None:
            raise ValueError('auralization routings are append-only')
        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_auralization_routing_declarations (
                    routing_id, routing_sha256, document_id,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    routing.routing_id,
                    routing.routing_sha256,
                    routing.document_id,
                    datetime.now(timezone.utc).isoformat(),
                    routing.model_dump_json(),
                ),
            )

    def get_routing(
        self, routing_id: str
    ) -> AuralizationRoutingDeclaration | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM '
                'cad_auralization_routing_declarations WHERE routing_id=?',
                (routing_id,),
            ).fetchone()
        if row is None:
            return None
        return AuralizationRoutingDeclaration.model_validate_json(
            row['payload_json']
        )

    def find_routing_by_sha(
        self, routing_sha256: str
    ) -> AuralizationRoutingDeclaration | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM '
                'cad_auralization_routing_declarations '
                'WHERE routing_sha256=?',
                (routing_sha256,),
            ).fetchone()
        if row is None:
            return None
        return AuralizationRoutingDeclaration.model_validate_json(
            row['payload_json']
        )

    # ------------------------------------------------------------------
    # Capability records (#538)
    # ------------------------------------------------------------------

    def save_capability(self, capability: AuralizationCapability) -> None:
        if self.get_capability(capability.capability_id) is not None:
            raise ValueError('auralization capabilities are append-only')
        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_auralization_capabilities (
                    capability_id, capability_semantic_sha256, spec_id,
                    document_id, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    capability.capability_id,
                    capability.semantic_sha256,
                    capability.spec_id,
                    capability.document_id,
                    datetime.now(timezone.utc).isoformat(),
                    capability.model_dump_json(),
                ),
            )

    def get_capability(
        self, capability_id: str
    ) -> AuralizationCapability | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_auralization_capabilities '
                'WHERE capability_id=?',
                (capability_id,),
            ).fetchone()
        if row is None:
            return None
        return AuralizationCapability.model_validate_json(row['payload_json'])

    def find_capability_by_sha(
        self, semantic_sha256: str
    ) -> AuralizationCapability | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_auralization_capabilities '
                'WHERE capability_semantic_sha256=?',
                (semantic_sha256,),
            ).fetchone()
        if row is None:
            return None
        return AuralizationCapability.model_validate_json(row['payload_json'])

    def capabilities_for_spec(
        self, spec_id: str
    ) -> tuple[AuralizationCapability, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_auralization_capabilities '
                'WHERE spec_id=? ORDER BY capability_id',
                (spec_id,),
            ).fetchall()
        return tuple(
            AuralizationCapability.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Listening validation records (#538)
    # ------------------------------------------------------------------

    def save_listening_validation(
        self, validation: MeasuredPredictedListeningValidation
    ) -> None:
        if (
            self.get_listening_validation(validation.validation_id)
            is not None
        ):
            raise ValueError('listening validations are append-only')
        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_auralization_listening_validations (
                    validation_id, validation_semantic_sha256, document_id,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    validation.validation_id,
                    validation.semantic_sha256,
                    validation.document_id,
                    datetime.now(timezone.utc).isoformat(),
                    validation.model_dump_json(),
                ),
            )

    def get_listening_validation(
        self, validation_id: str
    ) -> MeasuredPredictedListeningValidation | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM '
                'cad_auralization_listening_validations '
                'WHERE validation_id=?',
                (validation_id,),
            ).fetchone()
        if row is None:
            return None
        return MeasuredPredictedListeningValidation.model_validate_json(
            row['payload_json']
        )

    def listening_validations_for_document(
        self, document_id: str
    ) -> tuple[MeasuredPredictedListeningValidation, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_auralization_listening_validations '
                'WHERE document_id=? ORDER BY validation_id',
                (document_id,),
            ).fetchall()
        return tuple(
            MeasuredPredictedListeningValidation.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Review packages (#538)
    # ------------------------------------------------------------------

    def save_review_package(
        self, package: AuralizationReviewPackage, package_bytes: bytes
    ) -> None:
        if self.get_review_package(package.package_id) is not None:
            raise ValueError('auralization review packages are append-only')
        if sha256(package_bytes).hexdigest() != package.package_asset_sha256:
            raise ValueError(
                'review package bytes do not match package_asset_sha256'
            )
        # The package declares its own capabilities/routings; persistence
        # fails closed when those authorities were never recorded.
        for capability_payload in package.capability_payloads:
            capability = AuralizationCapability.model_validate(
                capability_payload
            )
            if self.get_capability(capability.capability_id) is None:
                raise ValueError(
                    'review package capability is not persisted: '
                    f'{capability.capability_id}'
                )
        self._asset_store.ensure_installed(
            package.package_asset_sha256, package_bytes
        )
        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_auralization_review_packages (
                    package_id, package_semantic_sha256, document_id,
                    package_asset_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    package.package_id,
                    package.semantic_sha256,
                    package.document_id,
                    package.package_asset_sha256,
                    datetime.now(timezone.utc).isoformat(),
                    package.model_dump_json(),
                ),
            )

    def get_review_package(
        self, package_id: str
    ) -> AuralizationReviewPackage | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_auralization_review_packages '
                'WHERE package_id=?',
                (package_id,),
            ).fetchone()
        if row is None:
            return None
        return AuralizationReviewPackage.model_validate_json(
            row['payload_json']
        )

    def review_packages_for_document(
        self, document_id: str
    ) -> tuple[AuralizationReviewPackage, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_auralization_review_packages '
                'WHERE document_id=? ORDER BY package_id',
                (document_id,),
            ).fetchall()
        return tuple(
            AuralizationReviewPackage.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    def read_review_package_bytes(
        self, package: AuralizationReviewPackage
    ) -> bytes:
        """Return the package zip bytes, re-verified against their digest."""

        raw = self._asset_store.read_verified(package.package_asset_sha256)
        if raw is None:
            raise ValueError(
                'review package bytes are missing from the managed '
                f'asset store: {package.package_asset_sha256}'
            )
        return raw


__all__ = ['CadAuralizationRepository']
