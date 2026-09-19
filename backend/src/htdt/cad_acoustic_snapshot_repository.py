from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from .cad_acoustic_snapshot import (
    AcousticPredictionRequest,
    AcousticSceneSnapshot,
    SurfaceBoundaryConfiguration,
    TreatmentBoundaryOverlaySnapshotRef,
    source_binding_from_r110,
)
from .cad_r110_source_repository import CadR110SourceRepository
from .cad_repository import SceneRepository
from .cad_scene import acoustic_reference_position
from .cad_schema import ensure_native_schema
from .cad_system_variant import materialize_system_variant
from .cad_system_variant_repository import CadSystemVariantRepository
from .cad_wave_excitation import CadWaveExcitationRepository
from .r120_geometry_compiler_repository import R120GeometryCompilerRepository
from .treatment_boundary_overlay_repository import TreatmentBoundaryOverlayRepository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CadAcousticSnapshotRepository:
    """Append-only exact AcousticSceneSnapshot and request persistence."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        variant_repository: CadSystemVariantRepository | None = None,
        r110_repository: CadR110SourceRepository | None = None,
        r120_repository: R120GeometryCompilerRepository | None = None,
        treatment_boundary_repository: TreatmentBoundaryOverlayRepository | None = None,
        wave_excitation_repository: CadWaveExcitationRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.variant_repository = (
            variant_repository
            if variant_repository is not None
            else CadSystemVariantRepository(scene_repository)
        )
        self.r110_repository = (
            r110_repository
            if r110_repository is not None
            else CadR110SourceRepository(
                scene_repository,
                variant_repository=self.variant_repository,
            )
        )
        self.r120_repository = (
            r120_repository
            if r120_repository is not None
            else R120GeometryCompilerRepository(scene_repository)
        )
        self.treatment_boundary_repository = treatment_boundary_repository
        self.wave_excitation_repository = (
            wave_excitation_repository
            if wave_excitation_repository is not None
            else CadWaveExcitationRepository(
                scene_repository,
                r110_repository=self.r110_repository,
            )
        )
        self.path = Path(scene_repository.path)
        if Path(self.wave_excitation_repository.path) != self.path:
            raise ValueError(
                'AcousticSceneSnapshot and wave-excitation repositories '
                'must share one native CAD database'
            )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cad_acoustic_scene_snapshots (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    scene_revision_id TEXT NOT NULL,
                    scene_content_hash TEXT NOT NULL,
                    system_variant_id TEXT,
                    system_variant_sha256 TEXT,
                    r120_compiled_geometry_id TEXT NOT NULL,
                    r120_compiled_geometry_sha256 TEXT NOT NULL,
                    material_boundary_configuration_sha256 TEXT NOT NULL,
                    environment_authority_sha256 TEXT,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL,
                    FOREIGN KEY(scene_revision_id)
                        REFERENCES scene_revisions(revision_id),
                    FOREIGN KEY(system_variant_id)
                        REFERENCES cad_system_variants(variant_id),
                    FOREIGN KEY(r120_compiled_geometry_id)
                        REFERENCES cad_r120_compiled_geometry(compiled_geometry_id)
                );
                CREATE INDEX IF NOT EXISTS idx_acoustic_snapshot_scene
                    ON cad_acoustic_scene_snapshots(scene_revision_id, seq ASC);

                CREATE TABLE IF NOT EXISTS cad_acoustic_prediction_requests (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    request_semantic_sha256 TEXT NOT NULL UNIQUE,
                    acoustic_scene_snapshot_id TEXT NOT NULL,
                    acoustic_scene_snapshot_sha256 TEXT NOT NULL,
                    deterministic_input_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL,
                    FOREIGN KEY(acoustic_scene_snapshot_id)
                        REFERENCES cad_acoustic_scene_snapshots(snapshot_id)
                );
                CREATE INDEX IF NOT EXISTS idx_acoustic_request_snapshot
                    ON cad_acoustic_prediction_requests(
                        acoustic_scene_snapshot_id, seq ASC
                    );
                """
            )

    def _validate_snapshot(
        self,
        snapshot: AcousticSceneSnapshot,
    ) -> AcousticSceneSnapshot:
        snapshot = AcousticSceneSnapshot.model_validate(
            snapshot.model_dump(mode='python')
        )

        revision = self.scene_repository.get(snapshot.scene_revision_id)
        if revision is None:
            raise ValueError(
                'AcousticSceneSnapshot references missing SceneRevision'
            )
        if (
            revision.document_id != snapshot.document_id
            or revision.content_hash != snapshot.scene_content_hash
        ):
            raise ValueError(
                'AcousticSceneSnapshot exact SceneRevision binding mismatch'
            )
        geometry = revision.document.r120_semantic_geometry
        if geometry is None:
            raise ValueError(
                'AcousticSceneSnapshot SceneRevision has no SemanticAcousticGeometry'
            )
        if (
            geometry.geometry_id != snapshot.semantic_geometry_id
            or geometry.semantic_hash_sha256
            != snapshot.semantic_geometry_sha256
        ):
            raise ValueError(
                'AcousticSceneSnapshot SemanticAcousticGeometry binding mismatch'
            )

        variant = None
        if snapshot.system_variant_id is not None:
            variant = self.variant_repository.get_variant(
                snapshot.system_variant_id
            )
            if variant is None:
                raise ValueError(
                    'AcousticSceneSnapshot references missing SystemVariant'
                )
            if variant.variant_sha256 != snapshot.system_variant_sha256:
                raise ValueError(
                    'AcousticSceneSnapshot SystemVariant hash mismatch'
                )
            if (
                variant.document_id != revision.document_id
                or variant.baseline_revision_id != revision.revision_id
                or variant.baseline_content_hash != revision.content_hash
            ):
                raise ValueError(
                    'AcousticSceneSnapshot SystemVariant/SceneRevision mismatch'
                )

        compiled = self.r120_repository.get_compiled_geometry(
            snapshot.r120_compiled_geometry_id
        )
        if compiled is None:
            raise ValueError(
                'AcousticSceneSnapshot references missing R120CompiledGeometry'
            )
        if (
            compiled.compiled_hash_sha256
            != snapshot.r120_compiled_geometry_sha256
            or compiled.topology_identity_sha256
            != snapshot.compiled_topology_sha256
            or compiled.exact_scene_revision_id != revision.revision_id
            or compiled.exact_scene_revision_content_hash
            != revision.content_hash
        ):
            raise ValueError(
                'AcousticSceneSnapshot R120CompiledGeometry binding mismatch'
            )
        if (
            compiled.exact_semantic_geometry_id != snapshot.semantic_geometry_id
            or compiled.exact_semantic_geometry_hash_sha256
            != snapshot.semantic_geometry_sha256
        ):
            raise ValueError(
                'AcousticSceneSnapshot R120/SemanticAcousticGeometry mismatch'
            )
        if (
            compiled.geometric_tolerance_m != snapshot.geometric_tolerance_m
            or compiled.approximation_error_bound_m
            != snapshot.approximation_error_bound_m
            or compiled.approximation_error_status
            != snapshot.approximation_error_status
            or compiled.maximum_dropped_feature_extent_m
            != snapshot.maximum_dropped_feature_extent_m
        ):
            raise ValueError(
                'AcousticSceneSnapshot R120 approximation metadata mismatch'
            )
        expected_surface_configuration = tuple(
            SurfaceBoundaryConfiguration(
                source_surface_id=item.source_surface_id,
                material_authority=item.material_authority,
                boundary_physics_authority=item.boundary_physics_authority,
            )
            for item in sorted(
                compiled.surface_mapping,
                key=lambda item: item.source_surface_id,
            )
        )
        if (
            expected_surface_configuration
            != snapshot.surface_boundary_configuration
        ):
            raise ValueError(
                'AcousticSceneSnapshot material/boundary configuration mismatch'
            )
        if compiled.region_authority_ref != snapshot.acoustic_region_authority_ref:
            raise ValueError(
                'AcousticSceneSnapshot AcousticRegion authority mismatch'
            )
        if compiled.portal_authority_ref != snapshot.portal_authority_ref:
            raise ValueError(
                'AcousticSceneSnapshot Portal authority mismatch'
            )
        if (
            compiled.boundary_termination_authority_ref
            != snapshot.boundary_termination_authority_ref
        ):
            raise ValueError(
                'AcousticSceneSnapshot BoundaryTermination authority mismatch'
            )

        if snapshot.treatment_boundary_bindings:
            if self.treatment_boundary_repository is None:
                raise ValueError(
                    'AcousticSceneSnapshot treatment bindings require typed '
                    'TreatmentBoundaryOverlayRepository resolution'
                )
            mapping_by_surface = {
                item.source_surface_id: item
                for item in compiled.surface_mapping
            }
            for binding in snapshot.treatment_boundary_bindings:
                mapping = mapping_by_surface.get(binding.host_surface_id)
                if mapping is None:
                    raise ValueError(
                        'AcousticSceneSnapshot treatment host surface is missing '
                        'from R120CompiledGeometry'
                    )
                if (
                    binding.base_material_authority != mapping.material_authority
                    or binding.base_boundary_physics_authority
                    != mapping.boundary_physics_authority
                ):
                    raise ValueError(
                        'AcousticSceneSnapshot treatment base boundary mismatch'
                    )

                resolved_overlays = []
                for overlay_binding in binding.attached_treatment_overlays:
                    overlay = self.treatment_boundary_repository.get_overlay(
                        overlay_binding.overlay_ref.authority_id
                    )
                    if overlay is None:
                        raise ValueError(
                            'AcousticSceneSnapshot references missing treatment overlay'
                        )
                    expected_overlay_binding = TreatmentBoundaryOverlaySnapshotRef(
                        overlay_ref=overlay.as_external_authority_ref(),
                        host_surface_authority_sha256=(
                            overlay.host_surface_authority_sha256
                        ),
                        lifecycle=overlay.lifecycle,
                        treatment_definition_id=overlay.treatment_definition_id,
                        treatment_definition_version=(
                            overlay.treatment_definition_version
                        ),
                        treatment_definition_hash_sha256=(
                            overlay.treatment_definition_hash_sha256
                        ),
                        treatment_placement_instance_id=(
                            overlay.treatment_placement_instance_id
                        ),
                        treatment_placement_version=(
                            overlay.treatment_placement_version
                        ),
                        treatment_placement_hash_sha256=(
                            overlay.treatment_placement_hash_sha256
                        ),
                        surface_binding_evaluation_id=(
                            overlay.surface_binding_evaluation_id
                        ),
                        surface_binding_evaluation_hash_sha256=(
                            overlay.surface_binding_evaluation_hash_sha256
                        ),
                        wave_capability_state=overlay.wave_capability_state,
                        geometric_capability_state=(
                            overlay.geometric_capability_state
                        ),
                    )
                    if (
                        expected_overlay_binding != overlay_binding
                        or overlay.host_surface_id != binding.host_surface_id
                        or overlay.exact_scene_revision_id != revision.revision_id
                        or overlay.exact_scene_revision_content_hash
                        != revision.content_hash
                        or overlay.exact_semantic_geometry_id != geometry.geometry_id
                        or overlay.exact_semantic_geometry_hash_sha256
                        != geometry.semantic_hash_sha256
                        or overlay.exact_r120_compiled_geometry_id
                        != compiled.compiled_geometry_id
                        or overlay.exact_r120_compiled_geometry_hash_sha256
                        != compiled.compiled_hash_sha256
                    ):
                        raise ValueError(
                            'AcousticSceneSnapshot treatment overlay exact identity mismatch'
                        )
                    resolved_overlays.append(overlay)

                if binding.status == 'AVAILABLE':
                    assert binding.composition_id is not None
                    composition = self.treatment_boundary_repository.get_composition(
                        binding.composition_id
                    )
                    if composition is None:
                        raise ValueError(
                            'AcousticSceneSnapshot references missing treatment composition'
                        )
                    if (
                        composition.composition_hash_sha256
                        != binding.composition_hash_sha256
                        or composition.authority_version
                        != binding.composition_authority_version
                        or composition.target_domain != binding.target_domain
                        or composition.host_surface_id != binding.host_surface_id
                        or composition.exact_scene_revision_id
                        != revision.revision_id
                        or composition.exact_scene_revision_content_hash
                        != revision.content_hash
                        or composition.exact_semantic_geometry_id
                        != geometry.geometry_id
                        or composition.exact_semantic_geometry_hash_sha256
                        != geometry.semantic_hash_sha256
                        or composition.exact_r120_compiled_geometry_id
                        != compiled.compiled_geometry_id
                        or composition.exact_r120_compiled_geometry_hash_sha256
                        != compiled.compiled_hash_sha256
                        or composition.selected_treatment_lifecycle
                        != binding.lifecycle
                        or composition.base_material_authority
                        != binding.base_material_authority
                        or composition.base_boundary_physics_authority
                        != binding.base_boundary_physics_authority
                        or composition.attached_treatment_overlays
                        != tuple(
                            item.overlay_ref
                            for item in binding.attached_treatment_overlays
                        )
                        or composition.selected_treatment_material_authorities
                        != binding.selected_treatment_material_authorities
                    ):
                        raise ValueError(
                            'AcousticSceneSnapshot treatment composition exact identity mismatch'
                        )
                    for overlay in resolved_overlays:
                        expected_material = (
                            overlay.wave_material_candidate_ref
                            if binding.target_domain == 'wave'
                            else overlay.geometric_material_candidate_ref
                        )
                        if (
                            expected_material is None
                            or expected_material
                            not in binding.selected_treatment_material_authorities
                            or overlay.lifecycle != binding.lifecycle
                        ):
                            raise ValueError(
                                'AcousticSceneSnapshot treatment composition '
                                'capability/lifecycle mismatch'
                            )
                elif binding.composition_id is not None:
                    raise ValueError(
                        'blocked AcousticSceneSnapshot treatment binding cannot '
                        'resolve as AVAILABLE composition'
                    )

        if snapshot.sources and variant is None:
            raise ValueError(
                'AcousticSceneSnapshot R110 sources require exact SystemVariant'
            )
        for binding in snapshot.sources:
            source = self.r110_repository.get_model(
                binding.r110_compiled_source_sha256
            )
            if source is None:
                raise ValueError(
                    'AcousticSceneSnapshot references missing R110 source authority'
                )
            if source_binding_from_r110(source) != binding:
                raise ValueError(
                    'AcousticSceneSnapshot R110 source exact identity mismatch'
                )
            if (
                source.scene_revision_id != revision.revision_id
                or source.scene_content_hash != revision.content_hash
            ):
                raise ValueError(
                    'AcousticSceneSnapshot R110 source SceneRevision mismatch'
                )
            if variant is not None and (
                source.system_variant_id != variant.variant_id
                or source.system_variant_sha256 != variant.variant_sha256
            ):
                raise ValueError(
                    'AcousticSceneSnapshot R110 source SystemVariant mismatch'
                )

        source_by_hash = {
            item.r110_compiled_source_sha256: item
            for item in snapshot.sources
        }
        for wave_binding in snapshot.wave_source_excitation_bindings:
            source_binding = source_by_hash.get(
                wave_binding.r110_compiled_source_sha256
            )
            if source_binding is None:
                raise ValueError(
                    'AcousticSceneSnapshot wave excitation references '
                    'missing R110 source'
                )
            resolved_binding = self.wave_excitation_repository.get_binding(
                wave_binding.binding_id
            )
            if resolved_binding is None:
                raise ValueError(
                    'AcousticSceneSnapshot references missing wave '
                    'excitation binding'
                )
            if resolved_binding != wave_binding:
                raise ValueError(
                    'AcousticSceneSnapshot wave excitation exact identity mismatch'
                )
            if (
                wave_binding.source_entity_id != source_binding.source_entity_id
                or wave_binding.equipment_definition_sha256
                != source_binding.equipment_definition_sha256
            ):
                raise ValueError(
                    'AcousticSceneSnapshot wave excitation source/equipment mismatch'
                )

        effective_scene = (
            revision.document
            if variant is None
            else materialize_system_variant(revision, variant)
        )
        for receiver in snapshot.receivers:
            try:
                entity = effective_scene.entity(receiver.entity_id)
            except KeyError as exc:
                raise ValueError(
                    'AcousticSceneSnapshot receiver entity is missing'
                ) from exc
            exact_position = acoustic_reference_position(entity)
            if exact_position is None or exact_position != receiver.world_position:
                raise ValueError(
                    'AcousticSceneSnapshot receiver reference position mismatch'
                )
            if (
                receiver.orientation is not None
                and receiver.orientation != entity.orientation
            ):
                raise ValueError(
                    'AcousticSceneSnapshot receiver orientation mismatch'
                )

        return snapshot

    def save_snapshot(
        self,
        snapshot: AcousticSceneSnapshot,
    ) -> AcousticSceneSnapshot:
        snapshot = self._validate_snapshot(snapshot)
        environment_hash = (
            None
            if snapshot.environment is None
            else snapshot.environment.authority.semantic_hash_sha256
        )

        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_scene_snapshots
                WHERE snapshot_id=?
                """,
                (snapshot.snapshot_id,),
            ).fetchone()
            if existing is not None:
                persisted = AcousticSceneSnapshot.model_validate_json(
                    existing['payload_json']
                )
                if persisted != snapshot:
                    raise ValueError(
                        'AcousticSceneSnapshot id already exists with different semantics'
                    )
                return self._validate_snapshot(persisted)

            collision = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_scene_snapshots
                WHERE semantic_sha256=?
                """,
                (snapshot.semantic_sha256,),
            ).fetchone()
            if collision is not None:
                persisted = AcousticSceneSnapshot.model_validate_json(
                    collision['payload_json']
                )
                if persisted != snapshot:
                    raise ValueError(
                        'AcousticSceneSnapshot hash collision with different semantics'
                    )
                return self._validate_snapshot(persisted)

            connection.execute(
                """
                INSERT INTO cad_acoustic_scene_snapshots(
                    snapshot_id,
                    semantic_sha256,
                    document_id,
                    scene_revision_id,
                    scene_content_hash,
                    system_variant_id,
                    system_variant_sha256,
                    r120_compiled_geometry_id,
                    r120_compiled_geometry_sha256,
                    material_boundary_configuration_sha256,
                    environment_authority_sha256,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.semantic_sha256,
                    snapshot.document_id,
                    snapshot.scene_revision_id,
                    snapshot.scene_content_hash,
                    snapshot.system_variant_id,
                    snapshot.system_variant_sha256,
                    snapshot.r120_compiled_geometry_id,
                    snapshot.r120_compiled_geometry_sha256,
                    snapshot.material_boundary_configuration_sha256,
                    environment_hash,
                    snapshot.model_dump_json(),
                    _utc_now(),
                ),
            )
        return snapshot

    def get_snapshot(
        self,
        snapshot_id: str,
    ) -> AcousticSceneSnapshot | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_scene_snapshots
                WHERE snapshot_id=?
                """,
                (snapshot_id,),
            ).fetchone()
        if row is None:
            return None
        snapshot = AcousticSceneSnapshot.model_validate_json(
            row['payload_json']
        )
        return self._validate_snapshot(snapshot)

    def get_snapshot_by_hash(
        self,
        semantic_sha256: str,
    ) -> AcousticSceneSnapshot | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_scene_snapshots
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        if row is None:
            return None
        snapshot = AcousticSceneSnapshot.model_validate_json(
            row['payload_json']
        )
        return self._validate_snapshot(snapshot)

    def save_prediction_request(
        self,
        request: AcousticPredictionRequest,
    ) -> AcousticPredictionRequest:
        request = AcousticPredictionRequest.model_validate(
            request.model_dump(mode='python')
        )
        snapshot = self.get_snapshot(
            request.acoustic_scene_snapshot_id
        )
        if snapshot is None:
            raise ValueError(
                'AcousticPredictionRequest references unpersisted AcousticSceneSnapshot'
            )
        if (
            snapshot.semantic_sha256
            != request.acoustic_scene_snapshot_sha256
        ):
            raise ValueError(
                'AcousticPredictionRequest snapshot hash mismatch'
            )

        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_prediction_requests
                WHERE request_id=?
                """,
                (request.request_id,),
            ).fetchone()
            if existing is not None:
                persisted = AcousticPredictionRequest.model_validate_json(
                    existing['payload_json']
                )
                if persisted != request:
                    raise ValueError(
                        'AcousticPredictionRequest id already exists with different semantics'
                    )
                return persisted

            connection.execute(
                """
                INSERT INTO cad_acoustic_prediction_requests(
                    request_id,
                    request_semantic_sha256,
                    acoustic_scene_snapshot_id,
                    acoustic_scene_snapshot_sha256,
                    deterministic_input_hash,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.request_semantic_sha256,
                    request.acoustic_scene_snapshot_id,
                    request.acoustic_scene_snapshot_sha256,
                    request.deterministic_input_hash,
                    request.model_dump_json(),
                    _utc_now(),
                ),
            )
        return request

    def get_prediction_request(
        self,
        request_id: str,
    ) -> AcousticPredictionRequest | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_prediction_requests
                WHERE request_id=?
                """,
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        request = AcousticPredictionRequest.model_validate_json(
            row['payload_json']
        )
        snapshot = self.get_snapshot(
            request.acoustic_scene_snapshot_id
        )
        if snapshot is None:
            raise ValueError(
                'persisted AcousticPredictionRequest references missing snapshot'
            )
        if (
            snapshot.semantic_sha256
            != request.acoustic_scene_snapshot_sha256
        ):
            raise ValueError(
                'persisted AcousticPredictionRequest snapshot hash mismatch'
            )
        return request
