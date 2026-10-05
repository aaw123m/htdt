"""Append-only persistence for RP32 commissioning authorities (#585).

Six tables preserve the full verification evidence chain:

* ``cad_rp32_profiles`` — sealed ``Rp32CommissioningProfile`` external
  profile authorities (which exact RP32 revision a project claimed).
* ``cad_rp32_reconciliations`` — sealed designed-vs-built reconciliations.
* ``cad_rp32_readiness`` — sealed readiness assessments.
* ``cad_rp32_verification_plans`` — sealed verification plans binding
  profile + design target + spatial campaign.
* ``cad_rp32_verification_records`` — sealed verification attempts; the
  ``prior_record_ids`` chain preserves failed baselines.
* ``cad_rp32_reports`` — sealed reproducible evidence packages.

Commit order enforces the evidence chain: a plan requires a persisted
profile; a readiness assessment and a record require a persisted plan; a
report requires a persisted record.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_rp32_profile import (
    AsBuiltReconciliation,
    CommissioningReport,
    Rp32CommissioningProfile,
    Rp32ReadinessAssessment,
    Rp32VerificationPlan,
    Rp32VerificationRecord,
)
from .cad_schema import connect_sqlite, require_native_tables


class Rp32ConflictError(ValueError):
    """An RP32 save violated append-only identity rules."""


class Rp32IntegrityError(ValueError):
    """A stored RP32 row disagreed with its payload."""


class CadRp32Repository:
    """Native storage for RP32 profiles, plans, records and reports."""

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
                'cad_rp32_profiles',
                'cad_rp32_reconciliations',
                'cad_rp32_readiness',
                'cad_rp32_verification_plans',
                'cad_rp32_verification_records',
                'cad_rp32_reports',
            )

    # ------------------------------------------------------------------
    # Profiles

    def save_profile(self, profile: Rp32CommissioningProfile) -> None:
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise Rp32ConflictError(
                'RP32 profiles are append-only — a different profile '
                'content is a different profile_id'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_rp32_profiles (
                    profile_id, profile_sha256, document_id,
                    publisher, revision, source_access_kind,
                    clause_mapping_state, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document.document_id,
                    profile.document.publisher,
                    profile.document.revision,
                    profile.document.source_access_kind,
                    profile.clause_mapping_state,
                    profile.created_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> Rp32CommissioningProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT profile_id, profile_sha256, document_id,
                       publisher, revision, source_access_kind,
                       clause_mapping_state, created_at_utc, payload_json
                FROM cad_rp32_profiles
                WHERE profile_id=?
                """,
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        return self._profile_from_row(row)

    def list_profiles(
        self,
    ) -> tuple[Rp32CommissioningProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT profile_id, profile_sha256, document_id,
                       publisher, revision, source_access_kind,
                       clause_mapping_state, created_at_utc, payload_json
                FROM cad_rp32_profiles
                ORDER BY created_at_utc, profile_id
                """,
            ).fetchall()
        return tuple(self._profile_from_row(row) for row in rows)

    def _profile_from_row(
        self, row: sqlite3.Row
    ) -> Rp32CommissioningProfile:
        profile = Rp32CommissioningProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document.document_id != row['document_id']
            or profile.document.publisher != row['publisher']
            or profile.document.revision != row['revision']
            or profile.document.source_access_kind
            != row['source_access_kind']
            or profile.clause_mapping_state != row['clause_mapping_state']
            or profile.created_at_utc != row['created_at_utc']
        ):
            raise Rp32IntegrityError(
                'RP32 profile row disagrees with its payload'
            )
        return profile

    # ------------------------------------------------------------------
    # Reconciliations

    def save_reconciliation(
        self, reconciliation: AsBuiltReconciliation
    ) -> None:
        existing = self.get_reconciliation(reconciliation.reconciliation_id)
        if existing is not None:
            if (
                existing.reconciliation_sha256
                == reconciliation.reconciliation_sha256
            ):
                return
            raise Rp32ConflictError(
                'as-built reconciliations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_rp32_reconciliations (
                    reconciliation_id, reconciliation_sha256, document_id,
                    state, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    reconciliation.reconciliation_id,
                    reconciliation.reconciliation_sha256,
                    reconciliation.document_id,
                    reconciliation.state,
                    reconciliation.evaluated_at_utc,
                    reconciliation.model_dump_json(),
                ),
            )

    def get_reconciliation(
        self, reconciliation_id: str
    ) -> AsBuiltReconciliation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT reconciliation_id, reconciliation_sha256,
                       document_id, state, evaluated_at_utc, payload_json
                FROM cad_rp32_reconciliations
                WHERE reconciliation_id=?
                """,
                (reconciliation_id,),
            ).fetchone()
        if row is None:
            return None
        return self._reconciliation_from_row(row)

    def _reconciliation_from_row(
        self, row: sqlite3.Row
    ) -> AsBuiltReconciliation:
        reconciliation = AsBuiltReconciliation.model_validate_json(
            row['payload_json']
        )
        if (
            reconciliation.reconciliation_id != row['reconciliation_id']
            or reconciliation.reconciliation_sha256
            != row['reconciliation_sha256']
            or reconciliation.document_id != row['document_id']
            or reconciliation.state != row['state']
            or reconciliation.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise Rp32IntegrityError(
                'reconciliation row disagrees with its payload'
            )
        return reconciliation

    # ------------------------------------------------------------------
    # Readiness assessments

    def save_readiness(
        self, assessment: Rp32ReadinessAssessment
    ) -> None:
        existing = self.get_readiness(assessment.assessment_id)
        if existing is not None:
            if existing.assessment_sha256 == assessment.assessment_sha256:
                return
            raise Rp32ConflictError(
                'readiness assessments are append-only'
            )
        plan = self.get_plan(assessment.plan_id)
        if plan is None:
            raise Rp32IntegrityError(
                'a readiness assessment must reference a persisted plan'
            )
        if plan.plan_sha256 != assessment.plan_sha256:
            raise Rp32IntegrityError(
                'readiness plan hash does not match the stored plan'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_rp32_readiness (
                    assessment_id, assessment_sha256, plan_id, plan_sha256,
                    document_id, state, assessed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment.assessment_id,
                    assessment.assessment_sha256,
                    assessment.plan_id,
                    assessment.plan_sha256,
                    assessment.document_id,
                    assessment.state,
                    assessment.assessed_at_utc,
                    assessment.model_dump_json(),
                ),
            )

    def get_readiness(
        self, assessment_id: str
    ) -> Rp32ReadinessAssessment | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT assessment_id, assessment_sha256, plan_id,
                       plan_sha256, document_id, state, assessed_at_utc,
                       payload_json
                FROM cad_rp32_readiness
                WHERE assessment_id=?
                """,
                (assessment_id,),
            ).fetchone()
        if row is None:
            return None
        return self._readiness_from_row(row)

    def _readiness_from_row(
        self, row: sqlite3.Row
    ) -> Rp32ReadinessAssessment:
        assessment = Rp32ReadinessAssessment.model_validate_json(
            row['payload_json']
        )
        if (
            assessment.assessment_id != row['assessment_id']
            or assessment.assessment_sha256 != row['assessment_sha256']
            or assessment.plan_id != row['plan_id']
            or assessment.plan_sha256 != row['plan_sha256']
            or assessment.document_id != row['document_id']
            or assessment.state != row['state']
            or assessment.assessed_at_utc != row['assessed_at_utc']
        ):
            raise Rp32IntegrityError(
                'readiness row disagrees with its payload'
            )
        return assessment

    # ------------------------------------------------------------------
    # Verification plans

    def save_plan(self, plan: Rp32VerificationPlan) -> None:
        existing = self.get_plan(plan.plan_id)
        if existing is not None:
            if existing.plan_sha256 == plan.plan_sha256:
                return
            raise Rp32ConflictError(
                'verification plans are append-only'
            )
        profile = self.get_profile(plan.profile_id)
        if profile is None:
            raise Rp32IntegrityError(
                'a verification plan must reference a persisted profile'
            )
        if profile.profile_sha256 != plan.profile_sha256:
            raise Rp32IntegrityError(
                'plan profile hash does not match the stored profile'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_rp32_verification_plans (
                    plan_id, plan_sha256, document_id, profile_id,
                    profile_sha256, spatial_design_id, created_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.plan_sha256,
                    plan.document_id,
                    plan.profile_id,
                    plan.profile_sha256,
                    plan.spatial_design_id,
                    plan.created_at_utc,
                    plan.model_dump_json(),
                ),
            )

    def get_plan(self, plan_id: str) -> Rp32VerificationPlan | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT plan_id, plan_sha256, document_id, profile_id,
                       profile_sha256, spatial_design_id, created_at_utc,
                       payload_json
                FROM cad_rp32_verification_plans
                WHERE plan_id=?
                """,
                (plan_id,),
            ).fetchone()
        if row is None:
            return None
        return self._plan_from_row(row)

    def plans_for_profile(
        self, profile_id: str
    ) -> tuple[Rp32VerificationPlan, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT plan_id, plan_sha256, document_id, profile_id,
                       profile_sha256, spatial_design_id, created_at_utc,
                       payload_json
                FROM cad_rp32_verification_plans
                WHERE profile_id=?
                ORDER BY created_at_utc, plan_id
                """,
                (profile_id,),
            ).fetchall()
        return tuple(self._plan_from_row(row) for row in rows)

    def _plan_from_row(self, row: sqlite3.Row) -> Rp32VerificationPlan:
        plan = Rp32VerificationPlan.model_validate_json(row['payload_json'])
        if (
            plan.plan_id != row['plan_id']
            or plan.plan_sha256 != row['plan_sha256']
            or plan.document_id != row['document_id']
            or plan.profile_id != row['profile_id']
            or plan.profile_sha256 != row['profile_sha256']
            or plan.spatial_design_id != row['spatial_design_id']
            or plan.created_at_utc != row['created_at_utc']
        ):
            raise Rp32IntegrityError(
                'verification plan row disagrees with its payload'
            )
        return plan

    # ------------------------------------------------------------------
    # Verification records

    def save_record(self, record: Rp32VerificationRecord) -> None:
        existing = self.get_record(record.record_id)
        if existing is not None:
            if existing.record_sha256 == record.record_sha256:
                return
            raise Rp32ConflictError(
                'verification records are append-only'
            )
        plan = self.get_plan(record.plan_id)
        if plan is None:
            raise Rp32IntegrityError(
                'a verification record must reference a persisted plan'
            )
        if plan.plan_sha256 != record.plan_sha256:
            raise Rp32IntegrityError(
                'record plan hash does not match the stored plan'
            )
        readiness = self.get_readiness(record.readiness_assessment_id)
        if readiness is None:
            raise Rp32IntegrityError(
                'a verification record must reference a persisted '
                'readiness assessment'
            )
        if record.reconciliation_id is not None:
            reconciliation = self.get_reconciliation(
                record.reconciliation_id
            )
            if reconciliation is None:
                raise Rp32IntegrityError(
                    'record references an unpersisted reconciliation'
                )
        for prior_id in record.prior_record_ids:
            if self.get_record(prior_id) is None:
                raise Rp32IntegrityError(
                    f'record chains to unpersisted prior record {prior_id}'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_rp32_verification_records (
                    record_id, record_sha256, plan_id, plan_sha256,
                    document_id, readiness_assessment_id, overall_state,
                    rp22_state, completed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.record_sha256,
                    record.plan_id,
                    record.plan_sha256,
                    record.document_id,
                    record.readiness_assessment_id,
                    record.overall_state,
                    record.rp22_state,
                    record.completed_at_utc,
                    record.model_dump_json(),
                ),
            )

    def get_record(self, record_id: str) -> Rp32VerificationRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT record_id, record_sha256, plan_id, plan_sha256,
                       document_id, readiness_assessment_id, overall_state,
                       rp22_state, completed_at_utc, payload_json
                FROM cad_rp32_verification_records
                WHERE record_id=?
                """,
                (record_id,),
            ).fetchone()
        if row is None:
            return None
        return self._record_from_row(row)

    def records_for_plan(
        self, plan_id: str
    ) -> tuple[Rp32VerificationRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT record_id, record_sha256, plan_id, plan_sha256,
                       document_id, readiness_assessment_id, overall_state,
                       rp22_state, completed_at_utc, payload_json
                FROM cad_rp32_verification_records
                WHERE plan_id=?
                ORDER BY completed_at_utc, record_id
                """,
                (plan_id,),
            ).fetchall()
        return tuple(self._record_from_row(row) for row in rows)

    def _record_from_row(
        self, row: sqlite3.Row
    ) -> Rp32VerificationRecord:
        record = Rp32VerificationRecord.model_validate_json(
            row['payload_json']
        )
        if (
            record.record_id != row['record_id']
            or record.record_sha256 != row['record_sha256']
            or record.plan_id != row['plan_id']
            or record.plan_sha256 != row['plan_sha256']
            or record.document_id != row['document_id']
            or record.readiness_assessment_id
            != row['readiness_assessment_id']
            or record.overall_state != row['overall_state']
            or record.rp22_state != row['rp22_state']
            or record.completed_at_utc != row['completed_at_utc']
        ):
            raise Rp32IntegrityError(
                'verification record row disagrees with its payload'
            )
        return record

    # ------------------------------------------------------------------
    # Reports

    def save_report(self, report: CommissioningReport) -> None:
        existing = self.get_report(report.report_id)
        if existing is not None:
            if existing.report_sha256 == report.report_sha256:
                return
            raise Rp32ConflictError(
                'commissioning reports are append-only'
            )
        record = self.get_record(report.record_id)
        if record is None:
            raise Rp32IntegrityError(
                'a commissioning report must reference a persisted record'
            )
        plan = self.get_plan(report.plan_id)
        if plan is None or record.plan_id != plan.plan_id:
            raise Rp32IntegrityError(
                'report plan does not match the stored record'
            )
        profile = self.get_profile(report.profile_id)
        if profile is None or profile.profile_id != plan.profile_id:
            raise Rp32IntegrityError(
                'report profile does not match the stored plan'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_rp32_reports (
                    report_id, report_sha256, document_id, profile_id,
                    profile_sha256, plan_id, record_id, overall_state,
                    generated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.report_id,
                    report.report_sha256,
                    report.document_id,
                    report.profile_id,
                    report.profile_sha256,
                    report.plan_id,
                    report.record_id,
                    report.overall_state,
                    report.generated_at_utc,
                    report.model_dump_json(),
                ),
            )

    def get_report(self, report_id: str) -> CommissioningReport | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT report_id, report_sha256, document_id, profile_id,
                       profile_sha256, plan_id, record_id, overall_state,
                       generated_at_utc, payload_json
                FROM cad_rp32_reports
                WHERE report_id=?
                """,
                (report_id,),
            ).fetchone()
        if row is None:
            return None
        return self._report_from_row(row)

    def reports_for_document(
        self, document_id: str
    ) -> tuple[CommissioningReport, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT report_id, report_sha256, document_id, profile_id,
                       profile_sha256, plan_id, record_id, overall_state,
                       generated_at_utc, payload_json
                FROM cad_rp32_reports
                WHERE document_id=?
                ORDER BY generated_at_utc, report_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._report_from_row(row) for row in rows)

    def _report_from_row(self, row: sqlite3.Row) -> CommissioningReport:
        report = CommissioningReport.model_validate_json(row['payload_json'])
        if (
            report.report_id != row['report_id']
            or report.report_sha256 != row['report_sha256']
            or report.document_id != row['document_id']
            or report.profile_id != row['profile_id']
            or report.profile_sha256 != row['profile_sha256']
            or report.plan_id != row['plan_id']
            or report.record_id != row['record_id']
            or report.overall_state != row['overall_state']
            or report.generated_at_utc != row['generated_at_utc']
        ):
            raise Rp32IntegrityError(
                'commissioning report row disagrees with its payload'
            )
        return report


__all__ = [
    'CadRp32Repository',
    'Rp32ConflictError',
    'Rp32IntegrityError',
]
