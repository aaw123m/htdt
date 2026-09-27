from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from .cad_acoustic_treatment_repository import CadAcousticTreatmentRepository
from .cad_repository import SceneRepository
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
    connect_sqlite,
)
from .r120_geometry_compiler import SurfaceBoundaryAuthorityBinding
from .r120_geometry_compiler_repository import R120GeometryCompilerRepository
from .treatment_boundary_overlay import (
    TreatmentBoundaryCompositionRequest,
    TreatmentBoundaryOverlay,
    compile_treatment_boundary_composition,
    compile_treatment_boundary_overlay,
)
from .clock import utc_now_iso as _utc_now


class TreatmentBoundaryOverlayRepository:
    """Append-only persistence for exact treatment overlays and boundary compositions."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        treatment_repository: CadAcousticTreatmentRepository,
        r120_repository: R120GeometryCompilerRepository,
    ) -> None:
        self.scene_repository = scene_repository
        self.treatment_repository = treatment_repository
        self.r120_repository = r120_repository
        self.path = Path(scene_repository.path)
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_treatment_boundary_overlays', 'cad_treatment_boundary_compositions')

    def save_overlay(self, overlay: TreatmentBoundaryOverlay) -> TreatmentBoundaryOverlay:
        overlay = TreatmentBoundaryOverlay.model_validate(overlay.model_dump(mode='python'))
        self._validate_overlay_bindings(overlay)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT payload_json FROM cad_treatment_boundary_overlays WHERE overlay_id=?',
                (overlay.overlay_id,),
            ).fetchone()
            if existing is not None:
                persisted = TreatmentBoundaryOverlay.model_validate_json(
                    existing['payload_json']
                )
                if persisted != overlay:
                    raise ValueError('treatment boundary overlay id collision')
                self._validate_overlay_bindings(persisted)
                return persisted
            hash_collision = connection.execute(
                'SELECT payload_json FROM cad_treatment_boundary_overlays '
                'WHERE overlay_hash_sha256=?',
                (overlay.overlay_hash_sha256,),
            ).fetchone()
            if hash_collision is not None:
                persisted = TreatmentBoundaryOverlay.model_validate_json(
                    hash_collision['payload_json']
                )
                if persisted != overlay:
                    raise ValueError('treatment boundary overlay hash collision')
                self._validate_overlay_bindings(persisted)
                return persisted
            connection.execute(
                """
                INSERT INTO cad_treatment_boundary_overlays(
                    overlay_id,
                    overlay_hash_sha256,
                    scene_revision_id,
                    compiled_geometry_id,
                    treatment_definition_id,
                    treatment_definition_version,
                    treatment_placement_instance_id,
                    treatment_placement_version,
                    surface_binding_evaluation_hash_sha256,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    overlay.overlay_id,
                    overlay.overlay_hash_sha256,
                    overlay.exact_scene_revision_id,
                    overlay.exact_r120_compiled_geometry_id,
                    overlay.treatment_definition_id,
                    overlay.treatment_definition_version,
                    overlay.treatment_placement_instance_id,
                    overlay.treatment_placement_version,
                    overlay.surface_binding_evaluation_hash_sha256,
                    overlay.model_dump_json(),
                    _utc_now(),
                ),
            )
        return overlay

    def get_overlay(self, overlay_id: str) -> TreatmentBoundaryOverlay | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT overlay_hash_sha256, payload_json '
                'FROM cad_treatment_boundary_overlays WHERE overlay_id=?',
                (overlay_id,),
            ).fetchone()
        if row is None:
            return None
        overlay = TreatmentBoundaryOverlay.model_validate_json(row['payload_json'])
        if (
            overlay.overlay_id != overlay_id
            or overlay.overlay_hash_sha256 != row['overlay_hash_sha256']
        ):
            raise ValueError(
                'persisted treatment boundary overlay identity mismatch'
            )
        self._validate_overlay_bindings(overlay)
        return overlay

    def save_composition(
        self,
        composition: TreatmentBoundaryCompositionRequest,
    ) -> TreatmentBoundaryCompositionRequest:
        composition = TreatmentBoundaryCompositionRequest.model_validate(
            composition.model_dump(mode='python')
        )
        self._validate_composition_bindings(composition)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT payload_json FROM cad_treatment_boundary_compositions '
                'WHERE composition_id=?',
                (composition.composition_id,),
            ).fetchone()
            if existing is not None:
                persisted = TreatmentBoundaryCompositionRequest.model_validate_json(
                    existing['payload_json']
                )
                if persisted != composition:
                    raise ValueError('treatment boundary composition id collision')
                self._validate_composition_bindings(persisted)
                return persisted
            hash_collision = connection.execute(
                'SELECT payload_json FROM cad_treatment_boundary_compositions '
                'WHERE composition_hash_sha256=?',
                (composition.composition_hash_sha256,),
            ).fetchone()
            if hash_collision is not None:
                persisted = TreatmentBoundaryCompositionRequest.model_validate_json(
                    hash_collision['payload_json']
                )
                if persisted != composition:
                    raise ValueError('treatment boundary composition hash collision')
                self._validate_composition_bindings(persisted)
                return persisted
            connection.execute(
                """
                INSERT INTO cad_treatment_boundary_compositions(
                    composition_id,
                    composition_hash_sha256,
                    scene_revision_id,
                    compiled_geometry_id,
                    host_surface_id,
                    target_domain,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    composition.composition_id,
                    composition.composition_hash_sha256,
                    composition.exact_scene_revision_id,
                    composition.exact_r120_compiled_geometry_id,
                    composition.host_surface_id,
                    composition.target_domain,
                    composition.model_dump_json(),
                    _utc_now(),
                ),
            )
        return composition

    def get_composition(
        self,
        composition_id: str,
    ) -> TreatmentBoundaryCompositionRequest | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT composition_hash_sha256, payload_json '
                'FROM cad_treatment_boundary_compositions '
                'WHERE composition_id=?',
                (composition_id,),
            ).fetchone()
        if row is None:
            return None
        composition = TreatmentBoundaryCompositionRequest.model_validate_json(
            row['payload_json']
        )
        if (
            composition.composition_id != composition_id
            or composition.composition_hash_sha256 != row['composition_hash_sha256']
        ):
            raise ValueError(
                'persisted treatment boundary composition identity mismatch'
            )
        self._validate_composition_bindings(composition)
        return composition

    def _validate_overlay_bindings(self, overlay: TreatmentBoundaryOverlay) -> None:
        revision = self.scene_repository.get(overlay.exact_scene_revision_id)
        if revision is None:
            raise ValueError('treatment overlay SceneRevision does not exist')
        if revision.content_hash != overlay.exact_scene_revision_content_hash:
            raise ValueError('treatment overlay SceneRevision hash mismatch')
        geometry = revision.document.r120_semantic_geometry
        if geometry is None:
            raise ValueError('treatment overlay SceneRevision has no SemanticAcousticGeometry')
        if (
            geometry.geometry_id != overlay.exact_semantic_geometry_id
            or geometry.semantic_hash_sha256
            != overlay.exact_semantic_geometry_hash_sha256
        ):
            raise ValueError('treatment overlay SemanticAcousticGeometry authority mismatch')

        compiled = self.r120_repository.get_compiled_geometry(
            overlay.exact_r120_compiled_geometry_id
        )
        if compiled is None:
            raise ValueError('treatment overlay R120CompiledGeometry does not exist')
        if compiled.compiled_hash_sha256 != overlay.exact_r120_compiled_geometry_hash_sha256:
            raise ValueError('treatment overlay R120CompiledGeometry hash mismatch')
        if (
            compiled.exact_scene_revision_id != revision.revision_id
            or compiled.exact_scene_revision_content_hash != revision.content_hash
            or compiled.exact_semantic_geometry_id != geometry.geometry_id
            or compiled.exact_semantic_geometry_hash_sha256
            != geometry.semantic_hash_sha256
        ):
            raise ValueError('treatment overlay references stale R120CompiledGeometry')
        if not any(
            item.source_surface_id == overlay.host_surface_id
            for item in compiled.surface_mapping
        ):
            raise ValueError('treatment overlay host surface is absent from R120CompiledGeometry')

        definition = self.treatment_repository.get_definition(
            overlay.treatment_definition_id,
            overlay.treatment_definition_version,
        )
        if definition is None:
            raise ValueError('treatment overlay definition does not exist')
        if definition.definition_sha256 != overlay.treatment_definition_hash_sha256:
            raise ValueError('treatment overlay definition hash mismatch')

        placement = self.treatment_repository.get_placement(
            overlay.treatment_placement_instance_id,
            overlay.treatment_placement_version,
        )
        if placement is None:
            raise ValueError('treatment overlay placement does not exist')
        if placement.placement_sha256 != overlay.treatment_placement_hash_sha256:
            raise ValueError('treatment overlay placement hash mismatch')
        if (
            placement.definition_id != definition.definition_id
            or placement.definition_version != definition.version
            or placement.definition_sha256 != definition.definition_sha256
            or placement.lifecycle != overlay.lifecycle
            or placement.host_surface_id != overlay.host_surface_id
            or placement.host_surface_authority_sha256
            != overlay.host_surface_authority_sha256
        ):
            raise ValueError('treatment overlay source authority mismatch')

        evaluation = self.treatment_repository.evaluate_placement_surface_binding(
            placement,
            scene_revision_id=revision.revision_id,
        )
        if (
            evaluation.evaluation_sha256
            != overlay.surface_binding_evaluation_hash_sha256
            or overlay.surface_binding_evaluation_id
            != f'treatment-surface-binding:{evaluation.evaluation_sha256}'
            or evaluation.binding_state != 'exact'
            or not evaluation.placement_authority_valid
        ):
            raise ValueError('treatment overlay surface binding is stale or mismatched')

        canonical = compile_treatment_boundary_overlay(
            definition,
            placement,
            evaluation,
            revision,
            compiled,
        )
        if canonical != overlay:
            raise ValueError(
                'treatment overlay is not reproducible from exact persisted authority'
            )

    def _validate_composition_bindings(
        self,
        composition: TreatmentBoundaryCompositionRequest,
    ) -> None:
        revision = self.scene_repository.get(composition.exact_scene_revision_id)
        if revision is None:
            raise ValueError('treatment composition SceneRevision does not exist')
        if revision.content_hash != composition.exact_scene_revision_content_hash:
            raise ValueError('treatment composition SceneRevision hash mismatch')
        geometry = revision.document.r120_semantic_geometry
        if geometry is None:
            raise ValueError('treatment composition SceneRevision has no SemanticAcousticGeometry')
        if (
            geometry.geometry_id != composition.exact_semantic_geometry_id
            or geometry.semantic_hash_sha256
            != composition.exact_semantic_geometry_hash_sha256
        ):
            raise ValueError('treatment composition SemanticAcousticGeometry mismatch')

        compiled = self.r120_repository.get_compiled_geometry(
            composition.exact_r120_compiled_geometry_id
        )
        if compiled is None:
            raise ValueError('treatment composition R120CompiledGeometry does not exist')
        if (
            compiled.compiled_hash_sha256
            != composition.exact_r120_compiled_geometry_hash_sha256
            or compiled.exact_scene_revision_id != revision.revision_id
            or compiled.exact_semantic_geometry_id != geometry.geometry_id
        ):
            raise ValueError('treatment composition references stale R120CompiledGeometry')

        resolved_overlays: list[TreatmentBoundaryOverlay] = []
        for overlay_ref in composition.attached_treatment_overlays:
            overlay = self.get_overlay(overlay_ref.authority_id)
            if overlay is None:
                raise ValueError('treatment composition references unpersisted overlay')
            if (
                overlay.overlay_hash_sha256 != overlay_ref.semantic_hash_sha256
                or overlay.authority_version != overlay_ref.authority_version
                or overlay.host_surface_id != composition.host_surface_id
                or overlay.exact_r120_compiled_geometry_id
                != composition.exact_r120_compiled_geometry_id
                or overlay.lifecycle != composition.selected_treatment_lifecycle
            ):
                raise ValueError('treatment composition overlay authority mismatch')
            resolved_overlays.append(overlay)

        if len(resolved_overlays) != 1:
            raise ValueError(
                'treatment composition must attach exactly one canonical overlay'
            )
        host_mappings = [
            item
            for item in compiled.surface_mapping
            if item.source_surface_id == composition.host_surface_id
        ]
        if len(host_mappings) != 1:
            raise ValueError(
                'treatment composition host surface is absent from '
                'R120CompiledGeometry'
            )
        host_mapping = host_mappings[0]
        canonical = compile_treatment_boundary_composition(
            resolved_overlays[0],
            target_domain=composition.target_domain,
            base_binding=SurfaceBoundaryAuthorityBinding(
                source_surface_id=host_mapping.source_surface_id,
                material_authority=host_mapping.material_authority,
                boundary_physics_authority=host_mapping.boundary_physics_authority,
            ),
        )
        if canonical != composition:
            raise ValueError(
                'treatment composition is not reproducible from exact persisted '
                'authority'
            )
