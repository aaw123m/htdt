"""Append-only persistence for the measurement-method reproducibility
authority (#693, REV58-MEASELEC).

Four tables:

* ``cad_method_procedures`` — sealed measurement-procedure declarations.
* ``cad_reproducibility_campaigns`` — sealed precision-study designs and
  their condition-attributed runs.
* ``cad_method_precision_models`` — sealed factor-attributed variance
  decompositions (within vs between kept separate).
* ``cad_reproducibility_qualifications`` — sealed fail-closed verdicts
  with the #566/#577 consequence gates.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_method_reproducibility import (
    CadMethodPrecisionModel,
    CadMethodProcedure,
    CadReproducibilityCampaign,
    CadReproducibilityQualification,
)


class ReproducibilityAuthorityConflictError(ValueError):
    """A reproducibility save violated append-only identity rules."""


class ReproducibilityAuthorityIntegrityError(ValueError):
    """A stored reproducibility row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise ReproducibilityAuthorityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise ReproducibilityAuthorityIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadMethodReproducibilityRepository:
    """Native storage for the #693 reproducibility authority records."""

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
                'cad_method_procedures',
                'cad_reproducibility_campaigns',
                'cad_method_precision_models',
                'cad_reproducibility_qualifications',
            )

    # ------------------------------------------------------------------
    # Method procedures

    def save_procedure(self, procedure: CadMethodProcedure) -> None:
        _assert_sealed(procedure, 'procedure_sha256', 'procedure_id')
        existing = self.get_procedure(procedure.procedure_id)
        if existing is not None:
            if existing.procedure_sha256 == procedure.procedure_sha256:
                return
            raise ReproducibilityAuthorityConflictError(
                'method procedures are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_method_procedures (
                    procedure_id, procedure_sha256, document_id,
                    method_name, procedure_version, documented,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    procedure.procedure_id,
                    procedure.procedure_sha256,
                    procedure.document_id,
                    procedure.method_name,
                    procedure.procedure_version,
                    1 if procedure.documented else 0,
                    procedure.declared_at_utc,
                    procedure.model_dump_json(),
                ),
            )

    def get_procedure(
        self, procedure_id: str
    ) -> CadMethodProcedure | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_method_procedures '
                'WHERE procedure_id=?',
                (procedure_id,),
            ).fetchone()
        if row is None:
            return None
        procedure = CadMethodProcedure.model_validate_json(
            row['payload_json']
        )
        if (
            procedure.procedure_id != row['procedure_id']
            or procedure.procedure_sha256 != row['procedure_sha256']
            or procedure.document_id != row['document_id']
            or procedure.method_name != row['method_name']
            or procedure.procedure_version != row['procedure_version']
            or (1 if procedure.documented else 0) != row['documented']
            or procedure.declared_at_utc != row['declared_at_utc']
        ):
            raise ReproducibilityAuthorityIntegrityError(
                'method procedure row disagrees with payload'
            )
        return procedure

    def list_procedures(
        self, document_id: str
    ) -> tuple[CadMethodProcedure, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_method_procedures '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadMethodProcedure.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Reproducibility campaigns

    def save_campaign(
        self, campaign: CadReproducibilityCampaign
    ) -> None:
        _assert_sealed(campaign, 'campaign_sha256', 'campaign_id')
        existing = self.get_campaign(campaign.campaign_id)
        if existing is not None:
            if existing.campaign_sha256 == campaign.campaign_sha256:
                return
            raise ReproducibilityAuthorityConflictError(
                'reproducibility campaigns are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_reproducibility_campaigns (
                    campaign_id, campaign_sha256, document_id,
                    procedure_ref_id, design_class, evidence_tier,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign.campaign_id,
                    campaign.campaign_sha256,
                    campaign.document_id,
                    campaign.procedure_ref.ref_id,
                    campaign.design_class,
                    campaign.evidence_tier,
                    campaign.declared_at_utc,
                    campaign.model_dump_json(),
                ),
            )

    def get_campaign(
        self, campaign_id: str
    ) -> CadReproducibilityCampaign | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_reproducibility_campaigns '
                'WHERE campaign_id=?',
                (campaign_id,),
            ).fetchone()
        if row is None:
            return None
        campaign = CadReproducibilityCampaign.model_validate_json(
            row['payload_json']
        )
        if (
            campaign.campaign_id != row['campaign_id']
            or campaign.campaign_sha256 != row['campaign_sha256']
            or campaign.document_id != row['document_id']
            or campaign.procedure_ref.ref_id != row['procedure_ref_id']
            or campaign.design_class != row['design_class']
            or campaign.evidence_tier != row['evidence_tier']
            or campaign.declared_at_utc != row['declared_at_utc']
        ):
            raise ReproducibilityAuthorityIntegrityError(
                'reproducibility campaign row disagrees with payload'
            )
        return campaign

    def list_campaigns(
        self, document_id: str
    ) -> tuple[CadReproducibilityCampaign, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_reproducibility_campaigns '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadReproducibilityCampaign.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Precision models

    def save_precision_model(
        self, model: CadMethodPrecisionModel
    ) -> None:
        _assert_sealed(model, 'model_sha256', 'model_id')
        existing = self.get_precision_model(model.model_id)
        if existing is not None:
            if existing.model_sha256 == model.model_sha256:
                return
            raise ReproducibilityAuthorityConflictError(
                'precision models are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_method_precision_models (
                    model_id, model_sha256, document_id,
                    campaign_ref_id, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    model.model_id,
                    model.model_sha256,
                    model.document_id,
                    model.campaign_ref.ref_id,
                    model.declared_at_utc,
                    model.model_dump_json(),
                ),
            )

    def get_precision_model(
        self, model_id: str
    ) -> CadMethodPrecisionModel | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_method_precision_models '
                'WHERE model_id=?',
                (model_id,),
            ).fetchone()
        if row is None:
            return None
        model = CadMethodPrecisionModel.model_validate_json(
            row['payload_json']
        )
        if (
            model.model_id != row['model_id']
            or model.model_sha256 != row['model_sha256']
            or model.document_id != row['document_id']
            or model.campaign_ref.ref_id != row['campaign_ref_id']
            or model.declared_at_utc != row['declared_at_utc']
        ):
            raise ReproducibilityAuthorityIntegrityError(
                'precision model row disagrees with payload'
            )
        return model

    def list_precision_models(
        self, document_id: str
    ) -> tuple[CadMethodPrecisionModel, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_method_precision_models '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadMethodPrecisionModel.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Reproducibility qualifications

    def save_qualification(
        self, qualification: CadReproducibilityQualification
    ) -> None:
        _assert_sealed(
            qualification, 'qualification_sha256', 'qualification_id'
        )
        existing = self.get_qualification(qualification.qualification_id)
        if existing is not None:
            if (
                existing.qualification_sha256
                == qualification.qualification_sha256
            ):
                return
            raise ReproducibilityAuthorityConflictError(
                'reproducibility qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_reproducibility_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    procedure_ref_id, evidence_tier, state,
                    evaluation_version, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.procedure_ref.ref_id,
                    qualification.evidence_tier,
                    qualification.state,
                    qualification.evaluation_version,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> CadReproducibilityQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_reproducibility_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = CadReproducibilityQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.procedure_ref.ref_id
            != row['procedure_ref_id']
            or qualification.evidence_tier != row['evidence_tier']
            or qualification.state != row['state']
            or qualification.evaluation_version
            != row['evaluation_version']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise ReproducibilityAuthorityIntegrityError(
                'reproducibility qualification row disagrees with payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str
    ) -> tuple[CadReproducibilityQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_reproducibility_qualifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadReproducibilityQualification.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadMethodReproducibilityRepository',
    'ReproducibilityAuthorityConflictError',
    'ReproducibilityAuthorityIntegrityError',
]
