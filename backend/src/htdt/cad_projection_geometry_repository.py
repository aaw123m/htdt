"""Append-only persistence for the projection image-geometry / masking
authority (#622).

Four tables:

* ``cad_presentation_geometry_bindings`` — sealed geometry bindings.
* ``cad_image_geometry_measurements`` — sealed geometry measurement
  campaigns.
* ``cad_lens_memory_recalls`` — sealed lens-memory recall records.
* ``cad_geometry_evaluations`` — sealed fail-closed geometry verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_projection_geometry_authority import (
    CadGeometryEvaluation,
    CadImageGeometryMeasurement,
    CadLensMemoryRecallRecord,
    CadPresentationGeometryBinding,
)


class ProjectionGeometryConflictError(ValueError):
    """A geometry-authority save violated append-only identity rules."""


class ProjectionGeometryIntegrityError(ValueError):
    """A stored geometry row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise ProjectionGeometryIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise ProjectionGeometryIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadProjectionGeometryRepository:
    """Native storage for the #622 geometry-authority records."""

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
                'cad_presentation_geometry_bindings',
                'cad_image_geometry_measurements',
                'cad_lens_memory_recalls',
                'cad_geometry_evaluations',
            )

    # ------------------------------------------------------------------
    # Geometry bindings

    def save_binding(
        self, binding: CadPresentationGeometryBinding
    ) -> None:
        _assert_sealed(binding, 'binding_sha256', 'binding_id')
        existing = self.get_binding(binding.binding_id)
        if existing is not None:
            if existing.binding_sha256 == binding.binding_sha256:
                return
            raise ProjectionGeometryConflictError(
                'geometry bindings are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_presentation_geometry_bindings (
                    binding_id, binding_sha256, document_id,
                    projected_aspect, content_aspect, keystone_state,
                    anamorphic_state, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding.binding_id,
                    binding.binding_sha256,
                    binding.document_id,
                    binding.projected_aspect,
                    binding.content_aspect,
                    binding.keystone_state,
                    binding.anamorphic_state,
                    binding.declared_at_utc,
                    binding.model_dump_json(),
                ),
            )

    def get_binding(
        self, binding_id: str
    ) -> CadPresentationGeometryBinding | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_presentation_geometry_bindings '
                'WHERE binding_id=?',
                (binding_id,),
            ).fetchone()
        if row is None:
            return None
        binding = CadPresentationGeometryBinding.model_validate_json(
            row['payload_json']
        )
        if (
            binding.binding_id != row['binding_id']
            or binding.binding_sha256 != row['binding_sha256']
            or binding.document_id != row['document_id']
            or binding.projected_aspect != row['projected_aspect']
            or binding.content_aspect != row['content_aspect']
            or binding.keystone_state != row['keystone_state']
            or binding.anamorphic_state != row['anamorphic_state']
            or binding.declared_at_utc != row['declared_at_utc']
        ):
            raise ProjectionGeometryIntegrityError(
                'geometry binding row disagrees with payload'
            )
        return binding

    def list_bindings(
        self, document_id: str
    ) -> tuple[CadPresentationGeometryBinding, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_presentation_geometry_bindings '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadPresentationGeometryBinding.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Geometry measurements

    def save_measurement(
        self, measurement: CadImageGeometryMeasurement
    ) -> None:
        _assert_sealed(
            measurement, 'measurement_sha256', 'measurement_id'
        )
        existing = self.get_measurement(measurement.measurement_id)
        if existing is not None:
            if (
                existing.measurement_sha256
                == measurement.measurement_sha256
            ):
                return
            raise ProjectionGeometryConflictError(
                'geometry measurements are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_image_geometry_measurements (
                    measurement_id, measurement_sha256, document_id,
                    binding_ref_id, method, test_pattern_identity,
                    physical_alignment, observed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    measurement.measurement_id,
                    measurement.measurement_sha256,
                    measurement.document_id,
                    measurement.binding_ref.ref_id,
                    measurement.method,
                    measurement.test_pattern_identity,
                    measurement.physical_alignment,
                    measurement.observed_at_utc,
                    measurement.model_dump_json(),
                ),
            )

    def get_measurement(
        self, measurement_id: str
    ) -> CadImageGeometryMeasurement | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_image_geometry_measurements '
                'WHERE measurement_id=?',
                (measurement_id,),
            ).fetchone()
        if row is None:
            return None
        measurement = CadImageGeometryMeasurement.model_validate_json(
            row['payload_json']
        )
        if (
            measurement.measurement_id != row['measurement_id']
            or measurement.measurement_sha256
            != row['measurement_sha256']
            or measurement.document_id != row['document_id']
            or measurement.binding_ref.ref_id != row['binding_ref_id']
            or measurement.method != row['method']
            or measurement.test_pattern_identity
            != row['test_pattern_identity']
            or measurement.physical_alignment
            != row['physical_alignment']
            or measurement.observed_at_utc != row['observed_at_utc']
        ):
            raise ProjectionGeometryIntegrityError(
                'geometry measurement row disagrees with payload'
            )
        return measurement

    def list_measurements(
        self, document_id: str
    ) -> tuple[CadImageGeometryMeasurement, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_image_geometry_measurements '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadImageGeometryMeasurement.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Lens-memory recalls

    def save_lens_recall(
        self, recall: CadLensMemoryRecallRecord
    ) -> None:
        _assert_sealed(recall, 'recall_sha256', 'recall_id')
        existing = self.get_lens_recall(recall.recall_id)
        if existing is not None:
            if existing.recall_sha256 == recall.recall_sha256:
                return
            raise ProjectionGeometryConflictError(
                'lens-memory recall records are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_lens_memory_recalls (
                    recall_id, recall_sha256, document_id, memory_id,
                    cycle_index, observed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recall.recall_id,
                    recall.recall_sha256,
                    recall.document_id,
                    recall.memory_id,
                    recall.cycle_index,
                    recall.observed_at_utc,
                    recall.model_dump_json(),
                ),
            )

    def get_lens_recall(
        self, recall_id: str
    ) -> CadLensMemoryRecallRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_lens_memory_recalls WHERE recall_id=?',
                (recall_id,),
            ).fetchone()
        if row is None:
            return None
        recall = CadLensMemoryRecallRecord.model_validate_json(
            row['payload_json']
        )
        if (
            recall.recall_id != row['recall_id']
            or recall.recall_sha256 != row['recall_sha256']
            or recall.document_id != row['document_id']
            or recall.memory_id != row['memory_id']
            or recall.cycle_index != row['cycle_index']
            or recall.observed_at_utc != row['observed_at_utc']
        ):
            raise ProjectionGeometryIntegrityError(
                'lens recall row disagrees with payload'
            )
        return recall

    def list_lens_recalls(
        self, document_id: str
    ) -> tuple[CadLensMemoryRecallRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_lens_memory_recalls '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadLensMemoryRecallRecord.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Geometry evaluations

    def save_evaluation(self, evaluation: CadGeometryEvaluation) -> None:
        _assert_sealed(
            evaluation, 'evaluation_sha256', 'evaluation_id'
        )
        existing = self.get_evaluation(evaluation.evaluation_id)
        if existing is not None:
            if existing.evaluation_sha256 == evaluation.evaluation_sha256:
                return
            raise ProjectionGeometryConflictError(
                'geometry evaluations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_geometry_evaluations (
                    evaluation_id, evaluation_sha256, document_id,
                    binding_ref_id, verdict, physical_alignment,
                    digital_correction_state, evaluation_version,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.evaluation_sha256,
                    evaluation.document_id,
                    evaluation.binding_ref.ref_id,
                    evaluation.verdict,
                    evaluation.physical_alignment,
                    evaluation.digital_correction_state,
                    evaluation.evaluation_version,
                    evaluation.evaluated_at_utc,
                    evaluation.model_dump_json(),
                ),
            )

    def get_evaluation(
        self, evaluation_id: str
    ) -> CadGeometryEvaluation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_geometry_evaluations '
                'WHERE evaluation_id=?',
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        evaluation = CadGeometryEvaluation.model_validate_json(
            row['payload_json']
        )
        if (
            evaluation.evaluation_id != row['evaluation_id']
            or evaluation.evaluation_sha256 != row['evaluation_sha256']
            or evaluation.document_id != row['document_id']
            or evaluation.binding_ref.ref_id != row['binding_ref_id']
            or evaluation.verdict != row['verdict']
            or evaluation.physical_alignment != row['physical_alignment']
            or evaluation.digital_correction_state
            != row['digital_correction_state']
            or evaluation.evaluation_version
            != row['evaluation_version']
            or evaluation.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise ProjectionGeometryIntegrityError(
                'geometry evaluation row disagrees with payload'
            )
        return evaluation

    def list_evaluations(
        self, document_id: str
    ) -> tuple[CadGeometryEvaluation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_geometry_evaluations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadGeometryEvaluation.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadProjectionGeometryRepository',
    'ProjectionGeometryConflictError',
    'ProjectionGeometryIntegrityError',
]
