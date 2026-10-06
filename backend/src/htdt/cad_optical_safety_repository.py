"""Append-only persistence for the projector optical-radiation safety
authority (#627).

Four tables:

* ``cad_projector_safety_identities`` — sealed safety identities (risk
  group + laser class kept distinct, each source-pinned).
* ``cad_manufacturer_safety_constraints`` — sealed manufacturer safety
  envelopes.
* ``cad_projector_placements`` — sealed placement declarations.
* ``cad_optical_safety_evaluations`` — sealed fail-closed safety
  verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_optical_safety_authority import (
    CadManufacturerSafetyConstraints,
    CadOpticalSafetyEvaluation,
    CadProjectorPlacementDeclaration,
    CadProjectorSafetyIdentity,
)


class OpticalSafetyConflictError(ValueError):
    """A safety-authority save violated append-only identity rules."""


class OpticalSafetyIntegrityError(ValueError):
    """A stored safety row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise OpticalSafetyIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise OpticalSafetyIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadOpticalSafetyRepository:
    """Native storage for the #627 optical-safety-authority records."""

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
                'cad_projector_safety_identities',
                'cad_manufacturer_safety_constraints',
                'cad_projector_placements',
                'cad_optical_safety_evaluations',
            )

    # ------------------------------------------------------------------
    # Safety identities

    def save_safety_identity(
        self, identity: CadProjectorSafetyIdentity
    ) -> None:
        _assert_sealed(identity, 'identity_sha256', 'identity_id')
        existing = self.get_safety_identity(identity.identity_id)
        if existing is not None:
            if existing.identity_sha256 == identity.identity_sha256:
                return
            raise OpticalSafetyConflictError(
                'projector safety identities are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_projector_safety_identities (
                    identity_id, identity_sha256, document_id,
                    illumination_source, risk_group, laser_class,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.identity_id,
                    identity.identity_sha256,
                    identity.document_id,
                    identity.illumination_source,
                    identity.risk_group,
                    identity.laser_class,
                    identity.declared_at_utc,
                    identity.model_dump_json(),
                ),
            )

    def get_safety_identity(
        self, identity_id: str
    ) -> CadProjectorSafetyIdentity | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_projector_safety_identities '
                'WHERE identity_id=?',
                (identity_id,),
            ).fetchone()
        if row is None:
            return None
        identity = CadProjectorSafetyIdentity.model_validate_json(
            row['payload_json']
        )
        if (
            identity.identity_id != row['identity_id']
            or identity.identity_sha256 != row['identity_sha256']
            or identity.document_id != row['document_id']
            or identity.illumination_source
            != row['illumination_source']
            or identity.risk_group != row['risk_group']
            or identity.laser_class != row['laser_class']
            or identity.declared_at_utc != row['declared_at_utc']
        ):
            raise OpticalSafetyIntegrityError(
                'safety identity row disagrees with payload'
            )
        return identity

    def list_safety_identities(
        self, document_id: str
    ) -> tuple[CadProjectorSafetyIdentity, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_projector_safety_identities '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadProjectorSafetyIdentity.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Manufacturer safety constraints

    def save_safety_constraints(
        self, constraints: CadManufacturerSafetyConstraints
    ) -> None:
        _assert_sealed(
            constraints, 'constraint_sha256', 'constraint_id'
        )
        existing = self.get_safety_constraints(
            constraints.constraint_id
        )
        if existing is not None:
            if existing.constraint_sha256 == constraints.constraint_sha256:
                return
            raise OpticalSafetyConflictError(
                'manufacturer safety constraints are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_manufacturer_safety_constraints (
                    constraint_id, constraint_sha256, document_id,
                    safety_identity_ref_id, source_document,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    constraints.constraint_id,
                    constraints.constraint_sha256,
                    constraints.document_id,
                    (
                        None
                        if constraints.safety_identity_ref is None
                        else constraints.safety_identity_ref.ref_id
                    ),
                    constraints.source_document,
                    constraints.declared_at_utc,
                    constraints.model_dump_json(),
                ),
            )

    def get_safety_constraints(
        self, constraint_id: str
    ) -> CadManufacturerSafetyConstraints | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_manufacturer_safety_constraints '
                'WHERE constraint_id=?',
                (constraint_id,),
            ).fetchone()
        if row is None:
            return None
        constraints = (
            CadManufacturerSafetyConstraints.model_validate_json(
                row['payload_json']
            )
        )
        if (
            constraints.constraint_id != row['constraint_id']
            or constraints.constraint_sha256
            != row['constraint_sha256']
            or constraints.document_id != row['document_id']
            or (
                None
                if constraints.safety_identity_ref is None
                else constraints.safety_identity_ref.ref_id
            ) != row['safety_identity_ref_id']
            or constraints.source_document != row['source_document']
            or constraints.declared_at_utc != row['declared_at_utc']
        ):
            raise OpticalSafetyIntegrityError(
                'safety constraints row disagrees with payload'
            )
        return constraints

    def list_safety_constraints(
        self, document_id: str
    ) -> tuple[CadManufacturerSafetyConstraints, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_manufacturer_safety_constraints '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadManufacturerSafetyConstraints.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Placements

    def save_placement(
        self, placement: CadProjectorPlacementDeclaration
    ) -> None:
        _assert_sealed(
            placement, 'placement_sha256', 'placement_id'
        )
        existing = self.get_placement(placement.placement_id)
        if existing is not None:
            if existing.placement_sha256 == placement.placement_sha256:
                return
            raise OpticalSafetyConflictError(
                'projector placements are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_projector_placements (
                    placement_id, placement_sha256, document_id,
                    safety_identity_ref_id, operating_state,
                    throw_distance_m, viewer_position,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    placement.placement_id,
                    placement.placement_sha256,
                    placement.document_id,
                    placement.safety_identity_ref.ref_id,
                    placement.operating_state,
                    placement.throw_distance_m,
                    placement.viewer_position,
                    placement.declared_at_utc,
                    placement.model_dump_json(),
                ),
            )

    def get_placement(
        self, placement_id: str
    ) -> CadProjectorPlacementDeclaration | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_projector_placements '
                'WHERE placement_id=?',
                (placement_id,),
            ).fetchone()
        if row is None:
            return None
        placement = (
            CadProjectorPlacementDeclaration.model_validate_json(
                row['payload_json']
            )
        )
        if (
            placement.placement_id != row['placement_id']
            or placement.placement_sha256 != row['placement_sha256']
            or placement.document_id != row['document_id']
            or placement.safety_identity_ref.ref_id
            != row['safety_identity_ref_id']
            or placement.operating_state != row['operating_state']
            or placement.throw_distance_m != row['throw_distance_m']
            or placement.viewer_position != row['viewer_position']
            or placement.declared_at_utc != row['declared_at_utc']
        ):
            raise OpticalSafetyIntegrityError(
                'placement row disagrees with payload'
            )
        return placement

    def list_placements(
        self, document_id: str
    ) -> tuple[CadProjectorPlacementDeclaration, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_projector_placements '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadProjectorPlacementDeclaration.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Safety evaluations

    def save_evaluation(
        self, evaluation: CadOpticalSafetyEvaluation
    ) -> None:
        _assert_sealed(
            evaluation, 'evaluation_sha256', 'evaluation_id'
        )
        existing = self.get_evaluation(evaluation.evaluation_id)
        if existing is not None:
            if existing.evaluation_sha256 == evaluation.evaluation_sha256:
                return
            raise OpticalSafetyConflictError(
                'optical safety evaluations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_optical_safety_evaluations (
                    evaluation_id, evaluation_sha256, document_id,
                    placement_ref_id, verdict, evaluation_version,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.evaluation_sha256,
                    evaluation.document_id,
                    evaluation.placement_ref.ref_id,
                    evaluation.verdict,
                    evaluation.evaluation_version,
                    evaluation.evaluated_at_utc,
                    evaluation.model_dump_json(),
                ),
            )

    def get_evaluation(
        self, evaluation_id: str
    ) -> CadOpticalSafetyEvaluation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_optical_safety_evaluations '
                'WHERE evaluation_id=?',
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        evaluation = CadOpticalSafetyEvaluation.model_validate_json(
            row['payload_json']
        )
        if (
            evaluation.evaluation_id != row['evaluation_id']
            or evaluation.evaluation_sha256 != row['evaluation_sha256']
            or evaluation.document_id != row['document_id']
            or evaluation.placement_ref.ref_id
            != row['placement_ref_id']
            or evaluation.verdict != row['verdict']
            or evaluation.evaluation_version
            != row['evaluation_version']
            or evaluation.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise OpticalSafetyIntegrityError(
                'safety evaluation row disagrees with payload'
            )
        return evaluation

    def list_evaluations(
        self, document_id: str
    ) -> tuple[CadOpticalSafetyEvaluation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_optical_safety_evaluations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadOpticalSafetyEvaluation.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadOpticalSafetyRepository',
    'OpticalSafetyConflictError',
    'OpticalSafetyIntegrityError',
]
