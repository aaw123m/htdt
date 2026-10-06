"""Append-only persistence for the spatial projection-image
qualification authority (#619).

Four tables:

* ``cad_spatial_measurement_plans`` — sealed spatial sampling plans.
* ``cad_spatial_measurement_sets`` — sealed canonical observation sets.
* ``cad_spatial_derived_maps`` — sealed interpolated-map provenance
  records (never canonical evidence).
* ``cad_spatial_uniformity_evaluations`` — sealed fail-closed
  uniformity verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_spatial_image_authority import (
    CadImageUniformityEvaluation,
    CadSpatialDerivedMap,
    CadSpatialMeasurementPlan,
    CadSpatialMeasurementSet,
)


class SpatialImageConflictError(ValueError):
    """A spatial-authority save violated append-only identity rules."""


class SpatialImageIntegrityError(ValueError):
    """A stored spatial row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise SpatialImageIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise SpatialImageIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadSpatialImageRepository:
    """Native storage for the #619 spatial-image-authority records."""

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
                'cad_spatial_measurement_plans',
                'cad_spatial_measurement_sets',
                'cad_spatial_derived_maps',
                'cad_spatial_uniformity_evaluations',
            )

    # ------------------------------------------------------------------
    # Measurement plans

    def save_plan(self, plan: CadSpatialMeasurementPlan) -> None:
        _assert_sealed(plan, 'plan_sha256', 'plan_id')
        existing = self.get_plan(plan.plan_id)
        if existing is not None:
            if existing.plan_sha256 == plan.plan_sha256:
                return
            raise SpatialImageConflictError(
                'spatial measurement plans are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_spatial_measurement_plans (
                    plan_id, plan_sha256, document_id, layout,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.plan_sha256,
                    plan.document_id,
                    plan.layout,
                    plan.declared_at_utc,
                    plan.model_dump_json(),
                ),
            )

    def get_plan(
        self, plan_id: str
    ) -> CadSpatialMeasurementPlan | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_spatial_measurement_plans '
                'WHERE plan_id=?',
                (plan_id,),
            ).fetchone()
        if row is None:
            return None
        plan = CadSpatialMeasurementPlan.model_validate_json(
            row['payload_json']
        )
        if (
            plan.plan_id != row['plan_id']
            or plan.plan_sha256 != row['plan_sha256']
            or plan.document_id != row['document_id']
            or plan.layout != row['layout']
            or plan.declared_at_utc != row['declared_at_utc']
        ):
            raise SpatialImageIntegrityError(
                'spatial plan row disagrees with payload'
            )
        return plan

    def list_plans(
        self, document_id: str
    ) -> tuple[CadSpatialMeasurementPlan, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_spatial_measurement_plans '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadSpatialMeasurementPlan.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Measurement sets

    def save_measurement_set(
        self, measurement_set: CadSpatialMeasurementSet
    ) -> None:
        _assert_sealed(measurement_set, 'set_sha256', 'set_id')
        existing = self.get_measurement_set(measurement_set.set_id)
        if existing is not None:
            if existing.set_sha256 == measurement_set.set_sha256:
                return
            raise SpatialImageConflictError(
                'spatial measurement sets are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_spatial_measurement_sets (
                    set_id, set_sha256, document_id, plan_ref_id,
                    evidence_kind, stimulus_profile, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    measurement_set.set_id,
                    measurement_set.set_sha256,
                    measurement_set.document_id,
                    measurement_set.plan_ref.ref_id,
                    measurement_set.evidence_kind,
                    measurement_set.stimulus_profile,
                    measurement_set.declared_at_utc,
                    measurement_set.model_dump_json(),
                ),
            )

    def get_measurement_set(
        self, set_id: str
    ) -> CadSpatialMeasurementSet | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_spatial_measurement_sets '
                'WHERE set_id=?',
                (set_id,),
            ).fetchone()
        if row is None:
            return None
        measurement_set = CadSpatialMeasurementSet.model_validate_json(
            row['payload_json']
        )
        if (
            measurement_set.set_id != row['set_id']
            or measurement_set.set_sha256 != row['set_sha256']
            or measurement_set.document_id != row['document_id']
            or measurement_set.plan_ref.ref_id != row['plan_ref_id']
            or measurement_set.evidence_kind != row['evidence_kind']
            or measurement_set.stimulus_profile
            != row['stimulus_profile']
            or measurement_set.declared_at_utc != row['declared_at_utc']
        ):
            raise SpatialImageIntegrityError(
                'measurement set row disagrees with payload'
            )
        return measurement_set

    def list_measurement_sets(
        self, document_id: str
    ) -> tuple[CadSpatialMeasurementSet, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_spatial_measurement_sets '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadSpatialMeasurementSet.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Derived maps

    def save_derived_map(self, derived_map: CadSpatialDerivedMap) -> None:
        _assert_sealed(derived_map, 'map_sha256', 'map_id')
        existing = self.get_derived_map(derived_map.map_id)
        if existing is not None:
            if existing.map_sha256 == derived_map.map_sha256:
                return
            raise SpatialImageConflictError(
                'spatial derived maps are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_spatial_derived_maps (
                    map_id, map_sha256, document_id, source_set_ref_id,
                    quantity, interpolation_algorithm, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    derived_map.map_id,
                    derived_map.map_sha256,
                    derived_map.document_id,
                    derived_map.source_set_ref.ref_id,
                    derived_map.quantity,
                    derived_map.interpolation_algorithm,
                    derived_map.declared_at_utc,
                    derived_map.model_dump_json(),
                ),
            )

    def get_derived_map(
        self, map_id: str
    ) -> CadSpatialDerivedMap | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_spatial_derived_maps WHERE map_id=?',
                (map_id,),
            ).fetchone()
        if row is None:
            return None
        derived_map = CadSpatialDerivedMap.model_validate_json(
            row['payload_json']
        )
        if (
            derived_map.map_id != row['map_id']
            or derived_map.map_sha256 != row['map_sha256']
            or derived_map.document_id != row['document_id']
            or derived_map.source_set_ref.ref_id
            != row['source_set_ref_id']
            or derived_map.quantity != row['quantity']
            or derived_map.interpolation_algorithm
            != row['interpolation_algorithm']
            or derived_map.declared_at_utc != row['declared_at_utc']
        ):
            raise SpatialImageIntegrityError(
                'derived map row disagrees with payload'
            )
        return derived_map

    def list_derived_maps(
        self, document_id: str
    ) -> tuple[CadSpatialDerivedMap, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_spatial_derived_maps '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadSpatialDerivedMap.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Uniformity evaluations

    def save_evaluation(
        self, evaluation: CadImageUniformityEvaluation
    ) -> None:
        _assert_sealed(
            evaluation, 'evaluation_sha256', 'evaluation_id'
        )
        existing = self.get_evaluation(evaluation.evaluation_id)
        if existing is not None:
            if existing.evaluation_sha256 == evaluation.evaluation_sha256:
                return
            raise SpatialImageConflictError(
                'spatial uniformity evaluations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_spatial_uniformity_evaluations (
                    evaluation_id, evaluation_sha256, document_id,
                    plan_ref_id, set_ref_id, coverage_state,
                    evaluation_version, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.evaluation_sha256,
                    evaluation.document_id,
                    evaluation.plan_ref.ref_id,
                    evaluation.set_ref.ref_id,
                    evaluation.coverage_state,
                    evaluation.evaluation_version,
                    evaluation.evaluated_at_utc,
                    evaluation.model_dump_json(),
                ),
            )

    def get_evaluation(
        self, evaluation_id: str
    ) -> CadImageUniformityEvaluation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_spatial_uniformity_evaluations '
                'WHERE evaluation_id=?',
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        evaluation = CadImageUniformityEvaluation.model_validate_json(
            row['payload_json']
        )
        if (
            evaluation.evaluation_id != row['evaluation_id']
            or evaluation.evaluation_sha256 != row['evaluation_sha256']
            or evaluation.document_id != row['document_id']
            or evaluation.plan_ref.ref_id != row['plan_ref_id']
            or evaluation.set_ref.ref_id != row['set_ref_id']
            or evaluation.coverage_state != row['coverage_state']
            or evaluation.evaluation_version
            != row['evaluation_version']
            or evaluation.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise SpatialImageIntegrityError(
                'uniformity evaluation row disagrees with payload'
            )
        return evaluation

    def list_evaluations(
        self, document_id: str
    ) -> tuple[CadImageUniformityEvaluation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_spatial_uniformity_evaluations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadImageUniformityEvaluation.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadSpatialImageRepository',
    'SpatialImageConflictError',
    'SpatialImageIntegrityError',
]
