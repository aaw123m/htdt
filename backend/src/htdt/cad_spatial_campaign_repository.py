"""Append-only persistence for spatial campaign authorities (#581).

Three tables:

* ``cad_spatial_campaign_designs`` — sealed ``SpatialCampaignDesign``
  authorities keyed by ``design_id``. Re-saving an identical row is a
  no-op; a divergent hash for the same id is a conflict — a declared
  sampling plan can never be silently revised.
* ``cad_spatial_campaign_evaluations`` — sealed
  ``CampaignDesignEvaluation`` verdicts.
* ``cad_spatial_campaign_bindings`` — capture-to-plan bindings
  (``CampaignPointBinding``) recording planned vs observed position.

Commit order is mechanically enforced: an evaluation or binding may only
be persisted against a design that is already stored — this is what makes
"declared before measurement" durable rather than a timestamp claim.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .cad_spatial_campaign import (
    CampaignDesignEvaluation,
    CampaignPointBinding,
    SpatialCampaignDesign,
)


class SpatialCampaignConflictError(ValueError):
    """A spatial campaign save violated append-only identity rules."""


class SpatialCampaignIntegrityError(ValueError):
    """A stored spatial campaign row disagreed with its payload."""


class CadSpatialCampaignRepository:
    """Native storage for spatial campaign designs/evaluations/bindings."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_spatial_campaign_designs',
                'cad_spatial_campaign_evaluations',
                'cad_spatial_campaign_bindings',
            )

    # ------------------------------------------------------------------
    # Designs

    def save_design(self, design: SpatialCampaignDesign) -> None:
        existing = self.get_design(design.design_id)
        if existing is not None:
            if existing.design_sha256 == design.design_sha256:
                return
            raise SpatialCampaignConflictError(
                'spatial campaign designs are append-only — a design_id '
                'with different content requires a new design'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_spatial_campaign_designs (
                    design_id, design_sha256, document_id,
                    scene_revision_id, scene_content_hash, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    design.design_id,
                    design.design_sha256,
                    design.document_id,
                    design.scene_revision_id,
                    design.scene_content_hash,
                    design.declared_at_utc,
                    design.model_dump_json(),
                ),
            )

    def get_design(self, design_id: str) -> SpatialCampaignDesign | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT design_id, design_sha256, document_id,
                       scene_revision_id, scene_content_hash,
                       declared_at_utc, payload_json
                FROM cad_spatial_campaign_designs
                WHERE design_id=?
                """,
                (design_id,),
            ).fetchone()
        if row is None:
            return None
        return self._design_from_row(row)

    def list_designs(
        self, document_id: str
    ) -> tuple[SpatialCampaignDesign, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT design_id, design_sha256, document_id,
                       scene_revision_id, scene_content_hash,
                       declared_at_utc, payload_json
                FROM cad_spatial_campaign_designs
                WHERE document_id=?
                ORDER BY declared_at_utc, design_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._design_from_row(row) for row in rows)

    def _design_from_row(self, row: sqlite3.Row) -> SpatialCampaignDesign:
        design = SpatialCampaignDesign.model_validate_json(
            row['payload_json']
        )
        if (
            design.design_id != row['design_id']
            or design.design_sha256 != row['design_sha256']
            or design.document_id != row['document_id']
            or design.scene_revision_id != row['scene_revision_id']
            or design.scene_content_hash != row['scene_content_hash']
            or design.declared_at_utc != row['declared_at_utc']
        ):
            raise SpatialCampaignIntegrityError(
                'spatial campaign design row disagrees with its payload'
            )
        return design

    # ------------------------------------------------------------------
    # Evaluations

    def save_evaluation(
        self, evaluation: CampaignDesignEvaluation
    ) -> None:
        existing = self.get_evaluation(evaluation.evaluation_id)
        if existing is not None:
            if existing.evaluation_sha256 == evaluation.evaluation_sha256:
                return
            raise SpatialCampaignConflictError(
                'campaign design evaluations are append-only'
            )
        design = self.get_design(evaluation.design_id)
        if design is None:
            raise SpatialCampaignIntegrityError(
                'an evaluation must reference a persisted design — '
                'declaring order is enforced by commit order'
            )
        if design.design_sha256 != evaluation.design_sha256:
            raise SpatialCampaignIntegrityError(
                'evaluation design hash does not match the stored design'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_spatial_campaign_evaluations (
                    evaluation_id, evaluation_sha256, design_id,
                    design_sha256, document_id, state, evaluated_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.evaluation_sha256,
                    evaluation.design_id,
                    evaluation.design_sha256,
                    evaluation.document_id,
                    evaluation.state,
                    evaluation.evaluated_at_utc,
                    evaluation.model_dump_json(),
                ),
            )

    def get_evaluation(
        self, evaluation_id: str
    ) -> CampaignDesignEvaluation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT evaluation_id, evaluation_sha256, design_id,
                       design_sha256, document_id, state,
                       evaluated_at_utc, payload_json
                FROM cad_spatial_campaign_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        return self._evaluation_from_row(row)

    def evaluations_for_design(
        self, design_id: str
    ) -> tuple[CampaignDesignEvaluation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT evaluation_id, evaluation_sha256, design_id,
                       design_sha256, document_id, state,
                       evaluated_at_utc, payload_json
                FROM cad_spatial_campaign_evaluations
                WHERE design_id=?
                ORDER BY evaluated_at_utc, evaluation_id
                """,
                (design_id,),
            ).fetchall()
        return tuple(self._evaluation_from_row(row) for row in rows)

    def _evaluation_from_row(
        self, row: sqlite3.Row
    ) -> CampaignDesignEvaluation:
        evaluation = CampaignDesignEvaluation.model_validate_json(
            row['payload_json']
        )
        if (
            evaluation.evaluation_id != row['evaluation_id']
            or evaluation.evaluation_sha256 != row['evaluation_sha256']
            or evaluation.design_id != row['design_id']
            or evaluation.design_sha256 != row['design_sha256']
            or evaluation.document_id != row['document_id']
            or evaluation.state != row['state']
            or evaluation.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise SpatialCampaignIntegrityError(
                'campaign evaluation row disagrees with its payload'
            )
        return evaluation

    # ------------------------------------------------------------------
    # Capture bindings

    def save_binding(self, binding: CampaignPointBinding) -> None:
        existing = self.get_binding(binding.binding_id)
        if existing is not None:
            if existing.binding_sha256 == binding.binding_sha256:
                return
            raise SpatialCampaignConflictError(
                'campaign point bindings are append-only'
            )
        design = self.get_design(binding.design_id)
        if design is None:
            raise SpatialCampaignIntegrityError(
                'a binding must reference a persisted design — a capture '
                'cannot be bound to a plan that was never declared'
            )
        if design.design_sha256 != binding.design_sha256:
            raise SpatialCampaignIntegrityError(
                'binding design hash does not match the stored design'
            )
        if design.point(binding.point_id) is None:
            raise SpatialCampaignIntegrityError(
                'binding names a point outside the stored design'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_spatial_campaign_bindings (
                    binding_id, binding_sha256, design_id, design_sha256,
                    document_id, point_id, measurement_id,
                    measurement_sha256, deviation_m, captured_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding.binding_id,
                    binding.binding_sha256,
                    binding.design_id,
                    binding.design_sha256,
                    binding.document_id,
                    binding.point_id,
                    binding.measurement_id,
                    binding.measurement_sha256,
                    binding.deviation_m,
                    binding.captured_at_utc,
                    binding.model_dump_json(),
                ),
            )

    def get_binding(
        self, binding_id: str
    ) -> CampaignPointBinding | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT binding_id, binding_sha256, design_id,
                       design_sha256, document_id, point_id,
                       measurement_id, measurement_sha256, deviation_m,
                       captured_at_utc, payload_json
                FROM cad_spatial_campaign_bindings
                WHERE binding_id=?
                """,
                (binding_id,),
            ).fetchone()
        if row is None:
            return None
        return self._binding_from_row(row)

    def bindings_for_design(
        self, design_id: str
    ) -> tuple[CampaignPointBinding, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT binding_id, binding_sha256, design_id,
                       design_sha256, document_id, point_id,
                       measurement_id, measurement_sha256, deviation_m,
                       captured_at_utc, payload_json
                FROM cad_spatial_campaign_bindings
                WHERE design_id=?
                ORDER BY captured_at_utc, binding_id
                """,
                (design_id,),
            ).fetchall()
        return tuple(self._binding_from_row(row) for row in rows)

    def bindings_for_measurement(
        self, measurement_id: str
    ) -> tuple[CampaignPointBinding, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT binding_id, binding_sha256, design_id,
                       design_sha256, document_id, point_id,
                       measurement_id, measurement_sha256, deviation_m,
                       captured_at_utc, payload_json
                FROM cad_spatial_campaign_bindings
                WHERE measurement_id=?
                ORDER BY captured_at_utc, binding_id
                """,
                (measurement_id,),
            ).fetchall()
        return tuple(self._binding_from_row(row) for row in rows)

    def _binding_from_row(
        self, row: sqlite3.Row
    ) -> CampaignPointBinding:
        binding = CampaignPointBinding.model_validate_json(
            row['payload_json']
        )
        if (
            binding.binding_id != row['binding_id']
            or binding.binding_sha256 != row['binding_sha256']
            or binding.design_id != row['design_id']
            or binding.design_sha256 != row['design_sha256']
            or binding.document_id != row['document_id']
            or binding.point_id != row['point_id']
            or binding.measurement_id != row['measurement_id']
            or binding.measurement_sha256 != row['measurement_sha256']
            or binding.captured_at_utc != row['captured_at_utc']
        ):
            raise SpatialCampaignIntegrityError(
                'campaign binding row disagrees with its payload'
            )
        return binding


__all__ = [
    'CadSpatialCampaignRepository',
    'SpatialCampaignConflictError',
    'SpatialCampaignIntegrityError',
]
