"""Append-only persistence for the porous-absorber authority (#615).

Five tables:

* ``cad_pam_parameter_evidence`` — sealed ``PorousParameterEvidence``
  records keyed by ``evidence_id``.
* ``cad_pam_material_models`` — sealed ``PorousMaterialModel`` records
  keyed by ``model_id``.
* ``cad_pam_buildups`` — sealed ``PorousBuildUp`` records keyed by
  ``buildup_id``.
* ``cad_pam_predictions`` — sealed ``PorousBoundaryPrediction`` records;
  a prediction may only persist against stored model + build-up rows
  whose shas match.
* ``cad_pam_fit_comparisons`` — sealed ``PorousFitComparison`` records; a
  bound prediction must persist with matching sha.

Re-saving an identical row is a no-op; a divergent hash under the same
id is a conflict. Records are sealed before insert — a ``model_copy``
forgery is rejected at the repository boundary.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_porous_absorber import (
    PorousBoundaryPrediction,
    PorousBuildUp,
    PorousFitComparison,
    PorousMaterialModel,
    PorousParameterEvidence,
)
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256


class PorousAbsorberConflictError(ValueError):
    """A porous-absorber save violated append-only identity rules."""


class PorousAbsorberIntegrityError(ValueError):
    """A stored porous-absorber row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha or getattr(record, id_field) != (
        getattr(record, id_field).split(':')[0] + ':' + sha
    ):
        raise PorousAbsorberIntegrityError(
            'record payload does not match its sealed identity'
        )


class CadPorousAbsorberRepository:
    """Native storage for porous-absorber authorities."""

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
                'cad_pam_parameter_evidence',
                'cad_pam_material_models',
                'cad_pam_buildups',
                'cad_pam_predictions',
                'cad_pam_fit_comparisons',
            )

    # ------------------------------------------------------------------
    # Parameter evidence

    def save_parameter(self, evidence: PorousParameterEvidence) -> None:
        _assert_sealed(evidence, 'evidence_sha256', 'evidence_id')
        existing = self.get_parameter(evidence.evidence_id)
        if existing is not None:
            if existing.evidence_sha256 == evidence.evidence_sha256:
                return
            raise PorousAbsorberConflictError(
                'parameter evidence is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_pam_parameter_evidence (
                    evidence_id, evidence_sha256, document_id,
                    material_ref, quantity, evidence_class, method,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_id,
                    evidence.evidence_sha256,
                    evidence.document_id,
                    evidence.material_ref,
                    evidence.quantity,
                    evidence.evidence_class,
                    evidence.method,
                    evidence.model_dump_json(),
                ),
            )

    def get_parameter(
        self, evidence_id: str
    ) -> PorousParameterEvidence | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT evidence_id, evidence_sha256, document_id,
                       material_ref, quantity, evidence_class, method,
                       payload_json
                FROM cad_pam_parameter_evidence
                WHERE evidence_id=?
                """,
                (evidence_id,),
            ).fetchone()
        if row is None:
            return None
        return self._parameter_from_row(row)

    def parameters_for_material(
        self, material_ref: str
    ) -> tuple[PorousParameterEvidence, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT evidence_id, evidence_sha256, document_id,
                       material_ref, quantity, evidence_class, method,
                       payload_json
                FROM cad_pam_parameter_evidence
                WHERE material_ref=?
                ORDER BY quantity, evidence_id
                """,
                (material_ref,),
            ).fetchall()
        return tuple(self._parameter_from_row(row) for row in rows)

    def _parameter_from_row(
        self, row: sqlite3.Row
    ) -> PorousParameterEvidence:
        evidence = PorousParameterEvidence.model_validate_json(
            row['payload_json']
        )
        if (
            evidence.evidence_id != row['evidence_id']
            or evidence.evidence_sha256 != row['evidence_sha256']
            or evidence.document_id != row['document_id']
            or evidence.material_ref != row['material_ref']
            or evidence.quantity != row['quantity']
            or evidence.evidence_class != row['evidence_class']
            or evidence.method != row['method']
        ):
            raise PorousAbsorberIntegrityError(
                'parameter evidence row disagrees with its payload'
            )
        return evidence

    # ------------------------------------------------------------------
    # Material models

    def save_model(self, model: PorousMaterialModel) -> None:
        _assert_sealed(model, 'model_sha256', 'model_id')
        existing = self.get_model(model.model_id)
        if existing is not None:
            if existing.model_sha256 == model.model_sha256:
                return
            raise PorousAbsorberConflictError(
                'porous material models are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_pam_material_models (
                    model_id, model_sha256, family, label, version,
                    compute_capable, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    model.model_id,
                    model.model_sha256,
                    model.family,
                    model.label,
                    model.version,
                    int(model.compute_capable),
                    model.model_dump_json(),
                ),
            )

    def get_model(self, model_id: str) -> PorousMaterialModel | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT model_id, model_sha256, family, label, version,
                       compute_capable, payload_json
                FROM cad_pam_material_models
                WHERE model_id=?
                """,
                (model_id,),
            ).fetchone()
        if row is None:
            return None
        model = PorousMaterialModel.model_validate_json(row['payload_json'])
        if (
            model.model_id != row['model_id']
            or model.model_sha256 != row['model_sha256']
            or model.family != row['family']
            or model.label != row['label']
            or model.version != row['version']
            or bool(model.compute_capable) != bool(row['compute_capable'])
        ):
            raise PorousAbsorberIntegrityError(
                'porous material model row disagrees with its payload'
            )
        return model

    def list_models(self) -> tuple[PorousMaterialModel, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT model_id, model_sha256, family, label, version,
                       compute_capable, payload_json
                FROM cad_pam_material_models
                ORDER BY model_id
                """,
            ).fetchall()
        return tuple(
            PorousMaterialModel.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Build-ups

    def save_buildup(self, buildup: PorousBuildUp) -> None:
        _assert_sealed(buildup, 'buildup_sha256', 'buildup_id')
        existing = self.get_buildup(buildup.buildup_id)
        if existing is not None:
            if existing.buildup_sha256 == buildup.buildup_sha256:
                return
            raise PorousAbsorberConflictError(
                'porous build-ups are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_pam_buildups (
                    buildup_id, buildup_sha256, document_id, label,
                    backing, anisotropy, layer_count, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    buildup.buildup_id,
                    buildup.buildup_sha256,
                    buildup.document_id,
                    buildup.label,
                    buildup.backing,
                    buildup.anisotropy,
                    len(buildup.layers),
                    buildup.model_dump_json(),
                ),
            )

    def get_buildup(self, buildup_id: str) -> PorousBuildUp | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT buildup_id, buildup_sha256, document_id, label,
                       backing, anisotropy, layer_count, payload_json
                FROM cad_pam_buildups
                WHERE buildup_id=?
                """,
                (buildup_id,),
            ).fetchone()
        if row is None:
            return None
        buildup = PorousBuildUp.model_validate_json(row['payload_json'])
        if (
            buildup.buildup_id != row['buildup_id']
            or buildup.buildup_sha256 != row['buildup_sha256']
            or buildup.document_id != row['document_id']
            or buildup.label != row['label']
            or buildup.backing != row['backing']
            or buildup.anisotropy != row['anisotropy']
            or len(buildup.layers) != row['layer_count']
        ):
            raise PorousAbsorberIntegrityError(
                'porous build-up row disagrees with its payload'
            )
        return buildup

    def list_buildups(
        self, document_id: str
    ) -> tuple[PorousBuildUp, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT buildup_id, buildup_sha256, document_id, label,
                       backing, anisotropy, layer_count, payload_json
                FROM cad_pam_buildups
                WHERE document_id=?
                ORDER BY buildup_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            PorousBuildUp.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Predictions

    def save_prediction(self, prediction: PorousBoundaryPrediction) -> None:
        _assert_sealed(prediction, 'prediction_sha256', 'prediction_id')
        existing = self.get_prediction(prediction.prediction_id)
        if existing is not None:
            if existing.prediction_sha256 == prediction.prediction_sha256:
                return
            raise PorousAbsorberConflictError(
                'porous predictions are append-only'
            )
        model = self.get_model(prediction.model_id)
        if model is None:
            raise PorousAbsorberIntegrityError(
                'a prediction must reference a persisted model'
            )
        if model.model_sha256 != prediction.model_sha256:
            raise PorousAbsorberIntegrityError(
                'prediction model hash does not match the stored model'
            )
        buildup = self.get_buildup(prediction.buildup_id)
        if buildup is None:
            raise PorousAbsorberIntegrityError(
                'a prediction must reference a persisted build-up'
            )
        if buildup.buildup_sha256 != prediction.buildup_sha256:
            raise PorousAbsorberIntegrityError(
                'prediction build-up hash does not match the stored build-up'
            )
        for evidence_id in prediction.parameter_evidence_ids:
            if self.get_parameter(evidence_id) is None:
                raise PorousAbsorberIntegrityError(
                    'a prediction must reference persisted parameters'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_pam_predictions (
                    prediction_id, prediction_sha256, document_id,
                    model_id, model_sha256, buildup_id, buildup_sha256,
                    eligibility, evidence_class, computed_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prediction.prediction_id,
                    prediction.prediction_sha256,
                    prediction.document_id,
                    prediction.model_id,
                    prediction.model_sha256,
                    prediction.buildup_id,
                    prediction.buildup_sha256,
                    prediction.eligibility,
                    prediction.evidence_class,
                    prediction.computed_at_utc,
                    prediction.model_dump_json(),
                ),
            )

    def get_prediction(
        self, prediction_id: str
    ) -> PorousBoundaryPrediction | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT prediction_id, prediction_sha256, document_id,
                       model_id, model_sha256, buildup_id, buildup_sha256,
                       eligibility, evidence_class, computed_at_utc,
                       payload_json
                FROM cad_pam_predictions
                WHERE prediction_id=?
                """,
                (prediction_id,),
            ).fetchone()
        if row is None:
            return None
        return self._prediction_from_row(row)

    def predictions_for_buildup(
        self, buildup_id: str
    ) -> tuple[PorousBoundaryPrediction, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT prediction_id, prediction_sha256, document_id,
                       model_id, model_sha256, buildup_id, buildup_sha256,
                       eligibility, evidence_class, computed_at_utc,
                       payload_json
                FROM cad_pam_predictions
                WHERE buildup_id=?
                ORDER BY computed_at_utc, prediction_id
                """,
                (buildup_id,),
            ).fetchall()
        return tuple(self._prediction_from_row(row) for row in rows)

    def _prediction_from_row(
        self, row: sqlite3.Row
    ) -> PorousBoundaryPrediction:
        prediction = PorousBoundaryPrediction.model_validate_json(
            row['payload_json']
        )
        if (
            prediction.prediction_id != row['prediction_id']
            or prediction.prediction_sha256 != row['prediction_sha256']
            or prediction.document_id != row['document_id']
            or prediction.model_id != row['model_id']
            or prediction.model_sha256 != row['model_sha256']
            or prediction.buildup_id != row['buildup_id']
            or prediction.buildup_sha256 != row['buildup_sha256']
            or prediction.eligibility != row['eligibility']
            or prediction.evidence_class != row['evidence_class']
            or prediction.computed_at_utc != row['computed_at_utc']
        ):
            raise PorousAbsorberIntegrityError(
                'porous prediction row disagrees with its payload'
            )
        return prediction

    # ------------------------------------------------------------------
    # Fit comparisons

    def save_comparison(self, comparison: PorousFitComparison) -> None:
        _assert_sealed(comparison, 'comparison_sha256', 'comparison_id')
        existing = self.get_comparison(comparison.comparison_id)
        if existing is not None:
            if existing.comparison_sha256 == comparison.comparison_sha256:
                return
            raise PorousAbsorberConflictError(
                'fit comparisons are append-only'
            )
        if comparison.prediction_id is not None:
            prediction = self.get_prediction(comparison.prediction_id)
            if prediction is None:
                raise PorousAbsorberIntegrityError(
                    'a fit comparison must reference a persisted prediction'
                )
            if prediction.prediction_sha256 != comparison.prediction_sha256:
                raise PorousAbsorberIntegrityError(
                    'fit comparison prediction hash does not match the '
                    'stored prediction'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_pam_fit_comparisons (
                    comparison_id, comparison_sha256, document_id,
                    prediction_id, prediction_sha256,
                    measured_evidence_ref, verdict, compared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    comparison.comparison_id,
                    comparison.comparison_sha256,
                    comparison.document_id,
                    comparison.prediction_id,
                    comparison.prediction_sha256,
                    comparison.measured_evidence_ref,
                    comparison.verdict,
                    comparison.compared_at_utc,
                    comparison.model_dump_json(),
                ),
            )

    def get_comparison(
        self, comparison_id: str
    ) -> PorousFitComparison | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT comparison_id, comparison_sha256, document_id,
                       prediction_id, prediction_sha256,
                       measured_evidence_ref, verdict, compared_at_utc,
                       payload_json
                FROM cad_pam_fit_comparisons
                WHERE comparison_id=?
                """,
                (comparison_id,),
            ).fetchone()
        if row is None:
            return None
        comparison = PorousFitComparison.model_validate_json(
            row['payload_json']
        )
        if (
            comparison.comparison_id != row['comparison_id']
            or comparison.comparison_sha256 != row['comparison_sha256']
            or comparison.document_id != row['document_id']
            or comparison.prediction_id != row['prediction_id']
            or comparison.prediction_sha256 != row['prediction_sha256']
            or comparison.measured_evidence_ref
            != row['measured_evidence_ref']
            or comparison.verdict != row['verdict']
            or comparison.compared_at_utc != row['compared_at_utc']
        ):
            raise PorousAbsorberIntegrityError(
                'fit comparison row disagrees with its payload'
            )
        return comparison
