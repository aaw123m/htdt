"""Append-only persistence for the safe-listening / test-exposure
authority (#602).

Five tables:

* ``cad_exposure_limits`` — sealed exposure limit profiles (basis,
  criterion, level/window/exchange, peak ceiling).
* ``cad_spl_capabilities`` — sealed SPL capability declarations
  (source-pinned or honestly estimated).
* ``cad_test_exposure_plans`` — sealed test-exposure plan declarations.
* ``cad_exposure_gates`` — sealed operator gate decisions.
* ``cad_exposure_assessments`` — sealed fail-closed exposure verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_safe_listening import (
    ExposureAssessment,
    ExposureGateDecision,
    ExposureLimitProfile,
    SplCapabilityDeclaration,
    TestExposurePlan,
)


class SafeListeningConflictError(ValueError):
    """A safe-listening save violated append-only identity rules."""


class SafeListeningIntegrityError(ValueError):
    """A stored safe-listening row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise SafeListeningIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise SafeListeningIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadSafeListeningRepository:
    """Native storage for the #602 exposure-authority records."""

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
                'cad_exposure_limits',
                'cad_spl_capabilities',
                'cad_test_exposure_plans',
                'cad_exposure_gates',
                'cad_exposure_assessments',
            )

    def _list(
        self,
        *,
        table: str,
        model,
        where: str,
        params: tuple[object, ...],
        order: str,
    ):
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f'SELECT payload_json FROM {table} WHERE {where} '
                f'ORDER BY {order}',
                params,
            ).fetchall()
        return tuple(
            model.model_validate_json(r['payload_json']) for r in rows
        )

    # ------------------------------------------------------------------
    # Exposure limits

    def save_limit(self, limit: ExposureLimitProfile) -> None:
        _assert_sealed(limit, 'limit_sha256', 'limit_id')
        existing = self.get_limit(limit.limit_id)
        if existing is not None:
            if existing.limit_sha256 == limit.limit_sha256:
                return
            raise SafeListeningConflictError(
                'exposure limits are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_exposure_limits (
                    limit_id, limit_sha256, document_id, label, basis,
                    criterion, limit_level_db, reference_window_s,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    limit.limit_id,
                    limit.limit_sha256,
                    limit.document_id,
                    limit.label,
                    limit.basis,
                    limit.criterion,
                    limit.limit_level_db,
                    limit.reference_window_s,
                    limit.declared_at_utc,
                    limit.model_dump_json(),
                ),
            )

    def get_limit(self, limit_id: str) -> ExposureLimitProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_exposure_limits WHERE limit_id=?',
                (limit_id,),
            ).fetchone()
        if row is None:
            return None
        limit = ExposureLimitProfile.model_validate_json(
            row['payload_json']
        )
        if (
            limit.limit_id != row['limit_id']
            or limit.limit_sha256 != row['limit_sha256']
            or limit.document_id != row['document_id']
            or limit.label != row['label']
            or limit.basis != row['basis']
            or limit.criterion != row['criterion']
            or limit.limit_level_db != row['limit_level_db']
            or limit.reference_window_s != row['reference_window_s']
            or limit.declared_at_utc != row['declared_at_utc']
        ):
            raise SafeListeningIntegrityError(
                'exposure limit row disagrees with payload'
            )
        return limit

    def list_limits(
        self, document_id: str
    ) -> tuple[ExposureLimitProfile, ...]:
        return self._list(
            table='cad_exposure_limits',
            model=ExposureLimitProfile,
            where='document_id=?',
            params=(document_id,),
            order='declared_at_utc, limit_id',
        )

    # ------------------------------------------------------------------
    # SPL capabilities

    def save_capability(
        self, capability: SplCapabilityDeclaration
    ) -> None:
        _assert_sealed(
            capability, 'capability_sha256', 'capability_id'
        )
        existing = self.get_capability(capability.capability_id)
        if existing is not None:
            if existing.capability_sha256 == capability.capability_sha256:
                return
            raise SafeListeningConflictError(
                'spl capabilities are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_spl_capabilities (
                    capability_id, capability_sha256, document_id,
                    scope, capability_source, source_ref_id,
                    max_continuous_db_spl, max_peak_db_spl,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capability.capability_id,
                    capability.capability_sha256,
                    capability.document_id,
                    capability.scope,
                    capability.capability_source,
                    capability.source_ref.ref_id
                    if capability.source_ref else None,
                    capability.max_continuous_db_spl,
                    capability.max_peak_db_spl,
                    capability.declared_at_utc,
                    capability.model_dump_json(),
                ),
            )

    def get_capability(
        self, capability_id: str
    ) -> SplCapabilityDeclaration | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_spl_capabilities '
                'WHERE capability_id=?',
                (capability_id,),
            ).fetchone()
        if row is None:
            return None
        capability = SplCapabilityDeclaration.model_validate_json(
            row['payload_json']
        )
        if (
            capability.capability_id != row['capability_id']
            or capability.capability_sha256 != row['capability_sha256']
            or capability.document_id != row['document_id']
            or capability.scope != row['scope']
            or capability.capability_source != row['capability_source']
            or capability.max_continuous_db_spl
            != row['max_continuous_db_spl']
            or capability.max_peak_db_spl != row['max_peak_db_spl']
            or capability.declared_at_utc != row['declared_at_utc']
        ):
            raise SafeListeningIntegrityError(
                'spl capability row disagrees with payload'
            )
        return capability

    def list_capabilities(
        self, document_id: str
    ) -> tuple[SplCapabilityDeclaration, ...]:
        return self._list(
            table='cad_spl_capabilities',
            model=SplCapabilityDeclaration,
            where='document_id=?',
            params=(document_id,),
            order='declared_at_utc, capability_id',
        )

    # ------------------------------------------------------------------
    # Test exposure plans

    def save_plan(self, plan: TestExposurePlan) -> None:
        _assert_sealed(plan, 'plan_sha256', 'plan_id')
        existing = self.get_plan(plan.plan_id)
        if existing is not None:
            if existing.plan_sha256 == plan.plan_sha256:
                return
            raise SafeListeningConflictError(
                'test exposure plans are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_test_exposure_plans (
                    plan_id, plan_sha256, document_id, label,
                    planned_level_db_spl, planned_duration_s, occupancy,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.plan_sha256,
                    plan.document_id,
                    plan.label,
                    plan.planned_level_db_spl,
                    plan.planned_duration_s,
                    plan.occupancy,
                    plan.declared_at_utc,
                    plan.model_dump_json(),
                ),
            )

    def get_plan(self, plan_id: str) -> TestExposurePlan | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_test_exposure_plans WHERE plan_id=?',
                (plan_id,),
            ).fetchone()
        if row is None:
            return None
        plan = TestExposurePlan.model_validate_json(row['payload_json'])
        if (
            plan.plan_id != row['plan_id']
            or plan.plan_sha256 != row['plan_sha256']
            or plan.document_id != row['document_id']
            or plan.label != row['label']
            or plan.planned_level_db_spl != row['planned_level_db_spl']
            or plan.planned_duration_s != row['planned_duration_s']
            or plan.occupancy != row['occupancy']
            or plan.declared_at_utc != row['declared_at_utc']
        ):
            raise SafeListeningIntegrityError(
                'test exposure plan row disagrees with payload'
            )
        return plan

    def list_plans(
        self, document_id: str
    ) -> tuple[TestExposurePlan, ...]:
        return self._list(
            table='cad_test_exposure_plans',
            model=TestExposurePlan,
            where='document_id=?',
            params=(document_id,),
            order='declared_at_utc, plan_id',
        )

    # ------------------------------------------------------------------
    # Exposure gates

    def save_gate(self, gate: ExposureGateDecision) -> None:
        _assert_sealed(gate, 'gate_sha256', 'gate_id')
        existing = self.get_gate(gate.gate_id)
        if existing is not None:
            if existing.gate_sha256 == gate.gate_sha256:
                return
            raise SafeListeningConflictError(
                'exposure gates are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_exposure_gates (
                    gate_id, gate_sha256, document_id,
                    assessment_ref_id, decision, decided_by,
                    decided_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    gate.gate_id,
                    gate.gate_sha256,
                    gate.document_id,
                    gate.assessment_ref.ref_id,
                    gate.decision,
                    gate.decided_by,
                    gate.decided_at_utc,
                    gate.model_dump_json(),
                ),
            )

    def get_gate(self, gate_id: str) -> ExposureGateDecision | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_exposure_gates WHERE gate_id=?',
                (gate_id,),
            ).fetchone()
        if row is None:
            return None
        gate = ExposureGateDecision.model_validate_json(
            row['payload_json']
        )
        if (
            gate.gate_id != row['gate_id']
            or gate.gate_sha256 != row['gate_sha256']
            or gate.document_id != row['document_id']
            or gate.decision != row['decision']
            or gate.decided_by != row['decided_by']
            or gate.decided_at_utc != row['decided_at_utc']
        ):
            raise SafeListeningIntegrityError(
                'exposure gate row disagrees with payload'
            )
        return gate

    def list_gates(
        self, document_id: str
    ) -> tuple[ExposureGateDecision, ...]:
        return self._list(
            table='cad_exposure_gates',
            model=ExposureGateDecision,
            where='document_id=?',
            params=(document_id,),
            order='decided_at_utc, gate_id',
        )

    # ------------------------------------------------------------------
    # Assessments

    def save_assessment(self, assessment: ExposureAssessment) -> None:
        _assert_sealed(
            assessment, 'assessment_sha256', 'assessment_id'
        )
        existing = self.get_assessment(assessment.assessment_id)
        if existing is not None:
            if existing.assessment_sha256 == assessment.assessment_sha256:
                return
            raise SafeListeningConflictError(
                'exposure assessments are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_exposure_assessments (
                    assessment_id, assessment_sha256, document_id,
                    plan_ref_id, limit_ref_id, state,
                    projected_dose_pct, evaluation_version,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment.assessment_id,
                    assessment.assessment_sha256,
                    assessment.document_id,
                    assessment.plan_ref.ref_id,
                    assessment.limit_ref.ref_id
                    if assessment.limit_ref else None,
                    assessment.state,
                    assessment.projected_dose_pct,
                    assessment.evaluation_version,
                    assessment.evaluated_at_utc,
                    assessment.model_dump_json(),
                ),
            )

    def get_assessment(
        self, assessment_id: str
    ) -> ExposureAssessment | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_exposure_assessments '
                'WHERE assessment_id=?',
                (assessment_id,),
            ).fetchone()
        if row is None:
            return None
        assessment = ExposureAssessment.model_validate_json(
            row['payload_json']
        )
        if (
            assessment.assessment_id != row['assessment_id']
            or assessment.assessment_sha256 != row['assessment_sha256']
            or assessment.document_id != row['document_id']
            or assessment.state != row['state']
            or assessment.projected_dose_pct != row['projected_dose_pct']
            or assessment.evaluation_version != row['evaluation_version']
            or assessment.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise SafeListeningIntegrityError(
                'exposure assessment row disagrees with payload'
            )
        return assessment

    def list_assessments(
        self, document_id: str
    ) -> tuple[ExposureAssessment, ...]:
        return self._list(
            table='cad_exposure_assessments',
            model=ExposureAssessment,
            where='document_id=?',
            params=(document_id,),
            order='evaluated_at_utc, assessment_id',
        )


__all__ = [
    'CadSafeListeningRepository',
    'SafeListeningConflictError',
    'SafeListeningIntegrityError',
]
