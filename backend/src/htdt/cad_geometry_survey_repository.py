"""Append-only persistence for the as-built geometry authority (#613).

Seven tables:

* ``cad_geo_survey_instruments`` — sealed ``GeometrySurveyInstrument``
  records keyed by ``instrument_id``.
* ``cad_geo_survey_campaigns`` — sealed ``SurveyCampaign`` records; a
  campaign may only reference persisted instruments.
* ``cad_geo_element_evidence`` — sealed ``GeometricElementEvidence``
  records; declared campaign refs must exist.
* ``cad_geo_control_measurements`` — sealed independent checks; a
  declared campaign or instrument ref must exist.
* ``cad_geo_reconciliations`` — sealed design-vs-as-built deltas.
* ``cad_geo_task_requirements`` — sealed declared-task requirements.
* ``cad_geo_qualifications`` — sealed ``AsBuiltGeometryQualification``
  verdicts; every referenced element/control/campaign must persist.

Re-saving an identical row is a no-op; a divergent hash under the same
id is a conflict. Records are sealed before insert — a ``model_copy``
forgery is rejected at the repository boundary.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_geometry_survey import (
    AsBuiltGeometryQualification,
    AsBuiltReconciliation,
    GeometricElementEvidence,
    GeometryControlMeasurement,
    GeometrySurveyInstrument,
    GeometryTaskRequirement,
    SurveyCampaign,
)
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256


class GeometrySurveyConflictError(ValueError):
    """A geometry-survey save violated append-only identity rules."""


class GeometrySurveyIntegrityError(ValueError):
    """A stored geometry-survey row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha or getattr(record, id_field) != (
        getattr(record, id_field).split(':')[0] + ':' + sha
    ):
        raise GeometrySurveyIntegrityError(
            'record payload does not match its sealed identity'
        )


class CadGeometrySurveyRepository:
    """Native storage for as-built geometry survey authorities."""

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
                'cad_geo_survey_instruments',
                'cad_geo_survey_campaigns',
                'cad_geo_element_evidence',
                'cad_geo_control_measurements',
                'cad_geo_reconciliations',
                'cad_geo_task_requirements',
                'cad_geo_qualifications',
            )

    # ------------------------------------------------------------------
    # Instruments

    def save_instrument(self, instrument: GeometrySurveyInstrument) -> None:
        _assert_sealed(instrument, 'instrument_sha256', 'instrument_id')
        existing = self.get_instrument(instrument.instrument_id)
        if existing is not None:
            if existing.instrument_sha256 == instrument.instrument_sha256:
                return
            raise GeometrySurveyConflictError(
                'survey instruments are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_geo_survey_instruments (
                    instrument_id, instrument_sha256, kind,
                    capability_class, manufacturer, model, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    instrument.instrument_id,
                    instrument.instrument_sha256,
                    instrument.kind,
                    instrument.capability_class,
                    instrument.manufacturer,
                    instrument.model,
                    instrument.model_dump_json(),
                ),
            )

    def get_instrument(
        self, instrument_id: str
    ) -> GeometrySurveyInstrument | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT instrument_id, instrument_sha256, kind,
                       capability_class, manufacturer, model, payload_json
                FROM cad_geo_survey_instruments
                WHERE instrument_id=?
                """,
                (instrument_id,),
            ).fetchone()
        if row is None:
            return None
        instrument = GeometrySurveyInstrument.model_validate_json(
            row['payload_json']
        )
        if (
            instrument.instrument_id != row['instrument_id']
            or instrument.instrument_sha256 != row['instrument_sha256']
            or instrument.kind != row['kind']
            or instrument.capability_class != row['capability_class']
            or instrument.manufacturer != row['manufacturer']
            or instrument.model != row['model']
        ):
            raise GeometrySurveyIntegrityError(
                'survey instrument row disagrees with its payload'
            )
        return instrument

    # ------------------------------------------------------------------
    # Campaigns

    def save_campaign(self, campaign: SurveyCampaign) -> None:
        _assert_sealed(campaign, 'campaign_sha256', 'campaign_id')
        existing = self.get_campaign(campaign.campaign_id)
        if existing is not None:
            if existing.campaign_sha256 == campaign.campaign_sha256:
                return
            raise GeometrySurveyConflictError(
                'survey campaigns are append-only'
            )
        for instrument_id in campaign.instrument_ids:
            if self.get_instrument(instrument_id) is None:
                raise GeometrySurveyIntegrityError(
                    'a campaign must reference persisted instruments'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_geo_survey_campaigns (
                    campaign_id, campaign_sha256, document_id, label,
                    captured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign.campaign_id,
                    campaign.campaign_sha256,
                    campaign.document_id,
                    campaign.label,
                    campaign.captured_at_utc,
                    campaign.model_dump_json(),
                ),
            )

    def get_campaign(self, campaign_id: str) -> SurveyCampaign | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT campaign_id, campaign_sha256, document_id, label,
                       captured_at_utc, payload_json
                FROM cad_geo_survey_campaigns
                WHERE campaign_id=?
                """,
                (campaign_id,),
            ).fetchone()
        if row is None:
            return None
        return self._campaign_from_row(row)

    def list_campaigns(self, document_id: str) -> tuple[SurveyCampaign, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT campaign_id, campaign_sha256, document_id, label,
                       captured_at_utc, payload_json
                FROM cad_geo_survey_campaigns
                WHERE document_id=?
                ORDER BY captured_at_utc, campaign_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._campaign_from_row(row) for row in rows)

    def _campaign_from_row(self, row: sqlite3.Row) -> SurveyCampaign:
        campaign = SurveyCampaign.model_validate_json(row['payload_json'])
        if (
            campaign.campaign_id != row['campaign_id']
            or campaign.campaign_sha256 != row['campaign_sha256']
            or campaign.document_id != row['document_id']
            or campaign.label != row['label']
            or campaign.captured_at_utc != row['captured_at_utc']
        ):
            raise GeometrySurveyIntegrityError(
                'survey campaign row disagrees with its payload'
            )
        return campaign

    # ------------------------------------------------------------------
    # Element evidence

    def save_element(self, element: GeometricElementEvidence) -> None:
        _assert_sealed(element, 'element_sha256', 'element_id')
        existing = self.get_element(element.element_id)
        if existing is not None:
            if existing.element_sha256 == element.element_sha256:
                return
            raise GeometrySurveyConflictError(
                'element evidence is append-only'
            )
        for campaign_id in element.campaign_ids:
            if self.get_campaign(campaign_id) is None:
                raise GeometrySurveyIntegrityError(
                    'element evidence must reference persisted campaigns'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_geo_element_evidence (
                    element_id, element_sha256, document_id, element_key,
                    observation_state, derivation_stage, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    element.element_id,
                    element.element_sha256,
                    element.document_id,
                    element.element_key,
                    element.observation_state,
                    element.derivation_stage,
                    element.model_dump_json(),
                ),
            )

    def get_element(
        self, element_id: str
    ) -> GeometricElementEvidence | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT element_id, element_sha256, document_id, element_key,
                       observation_state, derivation_stage, payload_json
                FROM cad_geo_element_evidence
                WHERE element_id=?
                """,
                (element_id,),
            ).fetchone()
        if row is None:
            return None
        return self._element_from_row(row)

    def list_elements(
        self, document_id: str
    ) -> tuple[GeometricElementEvidence, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT element_id, element_sha256, document_id, element_key,
                       observation_state, derivation_stage, payload_json
                FROM cad_geo_element_evidence
                WHERE document_id=?
                ORDER BY element_key, element_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._element_from_row(row) for row in rows)

    def _element_from_row(
        self, row: sqlite3.Row
    ) -> GeometricElementEvidence:
        element = GeometricElementEvidence.model_validate_json(
            row['payload_json']
        )
        if (
            element.element_id != row['element_id']
            or element.element_sha256 != row['element_sha256']
            or element.document_id != row['document_id']
            or element.element_key != row['element_key']
            or element.observation_state != row['observation_state']
            or element.derivation_stage != row['derivation_stage']
        ):
            raise GeometrySurveyIntegrityError(
                'element evidence row disagrees with its payload'
            )
        return element

    # ------------------------------------------------------------------
    # Control measurements

    def save_control(self, control: GeometryControlMeasurement) -> None:
        _assert_sealed(control, 'control_sha256', 'control_id')
        existing = self.get_control(control.control_id)
        if existing is not None:
            if existing.control_sha256 == control.control_sha256:
                return
            raise GeometrySurveyConflictError(
                'control measurements are append-only'
            )
        if (
            control.campaign_id is not None
            and self.get_campaign(control.campaign_id) is None
        ):
            raise GeometrySurveyIntegrityError(
                'a control must reference a persisted campaign'
            )
        if (
            control.instrument_id is not None
            and self.get_instrument(control.instrument_id) is None
        ):
            raise GeometrySurveyIntegrityError(
                'a control must reference a persisted instrument'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_geo_control_measurements (
                    control_id, control_sha256, document_id, kind,
                    campaign_id, instrument_id, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    control.control_id,
                    control.control_sha256,
                    control.document_id,
                    control.kind,
                    control.campaign_id,
                    control.instrument_id,
                    control.model_dump_json(),
                ),
            )

    def get_control(
        self, control_id: str
    ) -> GeometryControlMeasurement | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT control_id, control_sha256, document_id, kind,
                       campaign_id, instrument_id, payload_json
                FROM cad_geo_control_measurements
                WHERE control_id=?
                """,
                (control_id,),
            ).fetchone()
        if row is None:
            return None
        control = GeometryControlMeasurement.model_validate_json(
            row['payload_json']
        )
        if (
            control.control_id != row['control_id']
            or control.control_sha256 != row['control_sha256']
            or control.document_id != row['document_id']
            or control.kind != row['kind']
            or control.campaign_id != row['campaign_id']
            or control.instrument_id != row['instrument_id']
        ):
            raise GeometrySurveyIntegrityError(
                'control measurement row disagrees with its payload'
            )
        return control

    def list_controls(
        self, document_id: str
    ) -> tuple[GeometryControlMeasurement, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT control_id, control_sha256, document_id, kind,
                       campaign_id, instrument_id, payload_json
                FROM cad_geo_control_measurements
                WHERE document_id=?
                ORDER BY control_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            GeometryControlMeasurement.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Reconciliations

    def save_reconciliation(self, record: AsBuiltReconciliation) -> None:
        _assert_sealed(record, 'reconciliation_sha256', 'reconciliation_id')
        existing = self.get_reconciliation(record.reconciliation_id)
        if existing is not None:
            if existing.reconciliation_sha256 == record.reconciliation_sha256:
                return
            raise GeometrySurveyConflictError(
                'reconciliation records are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_geo_reconciliations (
                    reconciliation_id, reconciliation_sha256, document_id,
                    element_key, approved_change, reconciled_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.reconciliation_id,
                    record.reconciliation_sha256,
                    record.document_id,
                    record.element_key,
                    int(record.approved_change),
                    record.reconciled_at_utc,
                    record.model_dump_json(),
                ),
            )

    def get_reconciliation(
        self, reconciliation_id: str
    ) -> AsBuiltReconciliation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT reconciliation_id, reconciliation_sha256,
                       document_id, element_key, approved_change,
                       reconciled_at_utc, payload_json
                FROM cad_geo_reconciliations
                WHERE reconciliation_id=?
                """,
                (reconciliation_id,),
            ).fetchone()
        if row is None:
            return None
        record = AsBuiltReconciliation.model_validate_json(
            row['payload_json']
        )
        if (
            record.reconciliation_id != row['reconciliation_id']
            or record.reconciliation_sha256 != row['reconciliation_sha256']
            or record.document_id != row['document_id']
            or record.element_key != row['element_key']
            or bool(record.approved_change) != bool(row['approved_change'])
            or record.reconciled_at_utc != row['reconciled_at_utc']
        ):
            raise GeometrySurveyIntegrityError(
                'reconciliation row disagrees with its payload'
            )
        return record

    # ------------------------------------------------------------------
    # Task requirements

    def save_task_requirement(
        self, requirement: GeometryTaskRequirement
    ) -> None:
        _assert_sealed(requirement, 'task_sha256', 'task_id')
        existing = self.get_task_requirement(requirement.task_id)
        if existing is not None:
            if existing.task_sha256 == requirement.task_sha256:
                return
            raise GeometrySurveyConflictError(
                'task requirements are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_geo_task_requirements (
                    task_id, task_sha256, document_id, task_class,
                    tolerance_mm, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    requirement.task_id,
                    requirement.task_sha256,
                    requirement.document_id,
                    requirement.task_class,
                    requirement.tolerance_mm,
                    requirement.model_dump_json(),
                ),
            )

    def get_task_requirement(
        self, task_id: str
    ) -> GeometryTaskRequirement | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT task_id, task_sha256, document_id, task_class,
                       tolerance_mm, payload_json
                FROM cad_geo_task_requirements
                WHERE task_id=?
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        requirement = GeometryTaskRequirement.model_validate_json(
            row['payload_json']
        )
        if (
            requirement.task_id != row['task_id']
            or requirement.task_sha256 != row['task_sha256']
            or requirement.document_id != row['document_id']
            or requirement.task_class != row['task_class']
            or requirement.tolerance_mm != row['tolerance_mm']
        ):
            raise GeometrySurveyIntegrityError(
                'task requirement row disagrees with its payload'
            )
        return requirement

    def list_task_requirements(
        self, document_id: str
    ) -> tuple[GeometryTaskRequirement, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT task_id, task_sha256, document_id, task_class,
                       tolerance_mm, payload_json
                FROM cad_geo_task_requirements
                WHERE document_id=?
                ORDER BY task_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            GeometryTaskRequirement.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: AsBuiltGeometryQualification
    ) -> None:
        _assert_sealed(
            qualification, 'qualification_sha256', 'qualification_id'
        )
        existing = self.get_qualification(qualification.qualification_id)
        if existing is not None:
            if existing.qualification_sha256 == qualification.qualification_sha256:
                return
            raise GeometrySurveyConflictError(
                'geometry qualifications are append-only'
            )
        for state in qualification.element_states:
            if self.get_element(state.element_id) is None:
                raise GeometrySurveyIntegrityError(
                    'a qualification must reference persisted elements'
                )
        for campaign_id in qualification.campaign_ids:
            if self.get_campaign(campaign_id) is None:
                raise GeometrySurveyIntegrityError(
                    'a qualification must reference persisted campaigns'
                )
        for control_id in qualification.control_ids:
            if self.get_control(control_id) is None:
                raise GeometrySurveyIntegrityError(
                    'a qualification must reference persisted controls'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_geo_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    element_count, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    len(qualification.element_states),
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> AsBuiltGeometryQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT qualification_id, qualification_sha256, document_id,
                       element_count, evaluated_at_utc, payload_json
                FROM cad_geo_qualifications
                WHERE qualification_id=?
                """,
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = AsBuiltGeometryQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or len(qualification.element_states) != row['element_count']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise GeometrySurveyIntegrityError(
                'geometry qualification row disagrees with its payload'
            )
        return qualification

    def latest_qualification(
        self, document_id: str
    ) -> AsBuiltGeometryQualification | None:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT qualification_id, qualification_sha256, document_id,
                       element_count, evaluated_at_utc, payload_json
                FROM cad_geo_qualifications
                WHERE document_id=?
                ORDER BY seq DESC
                LIMIT 1
                """,
                (document_id,),
            ).fetchall()
        if not rows:
            return None
        row = rows[0]
        qualification = AsBuiltGeometryQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or len(qualification.element_states) != row['element_count']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise GeometrySurveyIntegrityError(
                'geometry qualification row disagrees with its payload'
            )
        return qualification
