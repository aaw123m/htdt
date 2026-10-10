"""Append-only persistence for the instrument calibration lifecycle
authority (#611).

Seven tables:

* ``cad_instrument_instances`` — physical instrument instances.
* ``cad_calibration_events`` — calibration/periodic-test events.
* ``cad_calibration_interval_policies`` — recalibration interval
  policies.
* ``cad_instrument_verification_checks`` — field/periodic checks.
* ``cad_instrument_service_events`` — damage/service events.
* ``cad_instrument_fitness_assessments`` — sealed fitness verdicts.
* ``cad_out_of_tolerance_reviews`` — non-destructive impact reviews.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from ...cad_repository import SceneRepository
from ...cad_schema import connect_sqlite, require_native_tables
from ...canonical_json import canonical_sha256
from ..domain.cad_calibration_lifecycle import (
    CadCalibrationEvent,
    CadCalibrationIntervalPolicy,
    CadInstrumentFitnessAssessment,
    CadInstrumentInstance,
    CadInstrumentServiceEvent,
    CadInstrumentVerificationCheck,
    CadOutOfToleranceReview,
)


class CalibrationLifecycleConflictError(ValueError):
    """A calibration-lifecycle save violated append-only rules."""


class CalibrationLifecycleIntegrityError(ValueError):
    """A stored calibration row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise CalibrationLifecycleIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise CalibrationLifecycleIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadCalibrationLifecycleRepository:
    """Native storage for the #611 calibration-lifecycle records."""

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
                'cad_instrument_instances',
                'cad_calibration_events',
                'cad_calibration_interval_policies',
                'cad_instrument_verification_checks',
                'cad_instrument_service_events',
                'cad_instrument_fitness_assessments',
                'cad_out_of_tolerance_reviews',
            )

    def _list(
        self,
        *,
        table: str,
        model,
        where: str,
        params: tuple[object, ...],
    ):
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f'SELECT payload_json FROM {table} WHERE {where} '
                'ORDER BY seq ASC',
                params,
            ).fetchall()
        return tuple(
            model.model_validate_json(r['payload_json']) for r in rows
        )

    # ------------------------------------------------------------------
    # Instrument instances

    def save_instrument(self, instrument: CadInstrumentInstance) -> None:
        _assert_sealed(
            instrument, 'instrument_sha256', 'instrument_id'
        )
        existing = self.get_instrument(instrument.instrument_id)
        if existing is not None:
            if existing.instrument_sha256 == instrument.instrument_sha256:
                return
            raise CalibrationLifecycleConflictError(
                'instrument instances are append-only — a changed unit '
                'is a new instance record'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_instrument_instances (
                    instrument_id, instrument_sha256, document_id,
                    category, serial_or_instance_id, service_state,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    instrument.instrument_id,
                    instrument.instrument_sha256,
                    instrument.document_id,
                    instrument.category,
                    instrument.serial_or_instance_id,
                    instrument.service_state,
                    instrument.declared_at_utc,
                    instrument.model_dump_json(),
                ),
            )

    def get_instrument(
        self, instrument_id: str
    ) -> CadInstrumentInstance | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_instrument_instances '
                'WHERE instrument_id=?',
                (instrument_id,),
            ).fetchone()
        if row is None:
            return None
        instrument = CadInstrumentInstance.model_validate_json(
            row['payload_json']
        )
        if (
            instrument.instrument_id != row['instrument_id']
            or instrument.instrument_sha256 != row['instrument_sha256']
            or instrument.document_id != row['document_id']
            or instrument.category != row['category']
            or instrument.serial_or_instance_id
            != row['serial_or_instance_id']
            or instrument.service_state != row['service_state']
            or instrument.declared_at_utc != row['declared_at_utc']
        ):
            raise CalibrationLifecycleIntegrityError(
                'instrument row disagrees with payload'
            )
        return instrument

    def list_instruments(
        self, document_id: str
    ) -> tuple[CadInstrumentInstance, ...]:
        return self._list(
            table='cad_instrument_instances',
            model=CadInstrumentInstance,
            where='document_id=?',
            params=(document_id,),
        )

    # ------------------------------------------------------------------
    # Calibration events

    def save_calibration(self, event: CadCalibrationEvent) -> None:
        _assert_sealed(event, 'calibration_sha256', 'calibration_id')
        existing = self.get_calibration(event.calibration_id)
        if existing is not None:
            if existing.calibration_sha256 == event.calibration_sha256:
                return
            raise CalibrationLifecycleConflictError(
                'calibration events are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_calibration_events (
                    calibration_id, calibration_sha256, document_id,
                    instrument_ref_id, event_kind, performed_at_utc,
                    provider_or_lab, traceability_class, valid_until_utc,
                    recorded_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.calibration_id,
                    event.calibration_sha256,
                    event.document_id,
                    event.instrument_ref.ref_id,
                    event.event_kind,
                    event.performed_at_utc,
                    event.provider_or_lab,
                    event.traceability_class,
                    event.valid_until_utc,
                    event.recorded_at_utc,
                    event.model_dump_json(),
                ),
            )

    def get_calibration(
        self, calibration_id: str
    ) -> CadCalibrationEvent | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_calibration_events '
                'WHERE calibration_id=?',
                (calibration_id,),
            ).fetchone()
        if row is None:
            return None
        event = CadCalibrationEvent.model_validate_json(
            row['payload_json']
        )
        if (
            event.calibration_id != row['calibration_id']
            or event.calibration_sha256 != row['calibration_sha256']
            or event.document_id != row['document_id']
            or event.instrument_ref.ref_id != row['instrument_ref_id']
            or event.event_kind != row['event_kind']
            or event.performed_at_utc != row['performed_at_utc']
            or event.provider_or_lab != row['provider_or_lab']
            or event.traceability_class != row['traceability_class']
            or event.valid_until_utc != row['valid_until_utc']
            or event.recorded_at_utc != row['recorded_at_utc']
        ):
            raise CalibrationLifecycleIntegrityError(
                'calibration row disagrees with payload'
            )
        return event

    def list_calibrations(
        self,
        document_id: str | None = None,
        *,
        instrument_id: str | None = None,
    ) -> tuple[CadCalibrationEvent, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if document_id is not None:
            clauses.append('document_id=?')
            params.append(document_id)
        if instrument_id is not None:
            clauses.append('instrument_ref_id=?')
            params.append(instrument_id)
        where = ' AND '.join(clauses) if clauses else '1=1'
        return self._list(
            table='cad_calibration_events',
            model=CadCalibrationEvent,
            where=where,
            params=tuple(params),
        )

    # ------------------------------------------------------------------
    # Interval policies

    def save_policy(self, policy: CadCalibrationIntervalPolicy) -> None:
        _assert_sealed(policy, 'policy_sha256', 'policy_id')
        existing = self.get_policy(policy.policy_id)
        if existing is not None:
            if existing.policy_sha256 == policy.policy_sha256:
                return
            raise CalibrationLifecycleConflictError(
                'interval policies are append-only — a revised policy '
                'is a new record'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_calibration_interval_policies (
                    policy_id, policy_sha256, document_id,
                    instrument_ref_id, instrument_category, basis,
                    nominal_interval_days, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy.policy_id,
                    policy.policy_sha256,
                    policy.document_id,
                    (
                        policy.instrument_ref.ref_id
                        if policy.instrument_ref is not None
                        else None
                    ),
                    policy.instrument_category,
                    policy.basis,
                    policy.nominal_interval_days,
                    policy.declared_at_utc,
                    policy.model_dump_json(),
                ),
            )

    def get_policy(
        self, policy_id: str
    ) -> CadCalibrationIntervalPolicy | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_calibration_interval_policies '
                'WHERE policy_id=?',
                (policy_id,),
            ).fetchone()
        if row is None:
            return None
        policy = CadCalibrationIntervalPolicy.model_validate_json(
            row['payload_json']
        )
        if (
            policy.policy_id != row['policy_id']
            or policy.policy_sha256 != row['policy_sha256']
            or policy.document_id != row['document_id']
            or (
                policy.instrument_ref.ref_id
                if policy.instrument_ref is not None else None
            ) != row['instrument_ref_id']
            or policy.instrument_category != row['instrument_category']
            or policy.basis != row['basis']
            or policy.nominal_interval_days
            != row['nominal_interval_days']
            or policy.declared_at_utc != row['declared_at_utc']
        ):
            raise CalibrationLifecycleIntegrityError(
                'policy row disagrees with payload'
            )
        return policy

    def list_policies(
        self, document_id: str
    ) -> tuple[CadCalibrationIntervalPolicy, ...]:
        return self._list(
            table='cad_calibration_interval_policies',
            model=CadCalibrationIntervalPolicy,
            where='document_id=?',
            params=(document_id,),
        )

    # ------------------------------------------------------------------
    # Verification checks

    def save_check(
        self, check: CadInstrumentVerificationCheck
    ) -> None:
        _assert_sealed(check, 'check_sha256', 'check_id')
        existing = self.get_check(check.check_id)
        if existing is not None:
            if existing.check_sha256 == check.check_sha256:
                return
            raise CalibrationLifecycleConflictError(
                'verification checks are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_instrument_verification_checks (
                    check_id, check_sha256, document_id,
                    instrument_ref_id, kind, outcome, campaign_id,
                    performed_at_utc, recorded_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    check.check_id,
                    check.check_sha256,
                    check.document_id,
                    check.instrument_ref.ref_id,
                    check.kind,
                    check.outcome,
                    check.campaign_id,
                    check.performed_at_utc,
                    check.recorded_at_utc,
                    check.model_dump_json(),
                ),
            )

    def get_check(
        self, check_id: str
    ) -> CadInstrumentVerificationCheck | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_instrument_verification_checks '
                'WHERE check_id=?',
                (check_id,),
            ).fetchone()
        if row is None:
            return None
        check = CadInstrumentVerificationCheck.model_validate_json(
            row['payload_json']
        )
        if (
            check.check_id != row['check_id']
            or check.check_sha256 != row['check_sha256']
            or check.document_id != row['document_id']
            or check.instrument_ref.ref_id != row['instrument_ref_id']
            or check.kind != row['kind']
            or check.outcome != row['outcome']
            or check.campaign_id != row['campaign_id']
            or check.performed_at_utc != row['performed_at_utc']
            or check.recorded_at_utc != row['recorded_at_utc']
        ):
            raise CalibrationLifecycleIntegrityError(
                'check row disagrees with payload'
            )
        return check

    def list_checks(
        self,
        document_id: str | None = None,
        *,
        instrument_id: str | None = None,
    ) -> tuple[CadInstrumentVerificationCheck, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if document_id is not None:
            clauses.append('document_id=?')
            params.append(document_id)
        if instrument_id is not None:
            clauses.append('instrument_ref_id=?')
            params.append(instrument_id)
        where = ' AND '.join(clauses) if clauses else '1=1'
        return self._list(
            table='cad_instrument_verification_checks',
            model=CadInstrumentVerificationCheck,
            where=where,
            params=tuple(params),
        )

    # ------------------------------------------------------------------
    # Service events

    def save_service_event(
        self, event: CadInstrumentServiceEvent
    ) -> None:
        _assert_sealed(event, 'event_sha256', 'event_id')
        existing = self.get_service_event(event.event_id)
        if existing is not None:
            if existing.event_sha256 == event.event_sha256:
                return
            raise CalibrationLifecycleConflictError(
                'service events are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_instrument_service_events (
                    event_id, event_sha256, document_id,
                    instrument_ref_id, kind, occurred_at_utc,
                    recorded_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.event_sha256,
                    event.document_id,
                    event.instrument_ref.ref_id,
                    event.kind,
                    event.occurred_at_utc,
                    event.recorded_at_utc,
                    event.model_dump_json(),
                ),
            )

    def get_service_event(
        self, event_id: str
    ) -> CadInstrumentServiceEvent | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_instrument_service_events '
                'WHERE event_id=?',
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        event = CadInstrumentServiceEvent.model_validate_json(
            row['payload_json']
        )
        if (
            event.event_id != row['event_id']
            or event.event_sha256 != row['event_sha256']
            or event.document_id != row['document_id']
            or event.instrument_ref.ref_id != row['instrument_ref_id']
            or event.kind != row['kind']
            or event.occurred_at_utc != row['occurred_at_utc']
            or event.recorded_at_utc != row['recorded_at_utc']
        ):
            raise CalibrationLifecycleIntegrityError(
                'service event row disagrees with payload'
            )
        return event

    def list_service_events(
        self,
        document_id: str | None = None,
        *,
        instrument_id: str | None = None,
    ) -> tuple[CadInstrumentServiceEvent, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if document_id is not None:
            clauses.append('document_id=?')
            params.append(document_id)
        if instrument_id is not None:
            clauses.append('instrument_ref_id=?')
            params.append(instrument_id)
        where = ' AND '.join(clauses) if clauses else '1=1'
        return self._list(
            table='cad_instrument_service_events',
            model=CadInstrumentServiceEvent,
            where=where,
            params=tuple(params),
        )

    # ------------------------------------------------------------------
    # Fitness assessments

    def save_assessment(
        self, assessment: CadInstrumentFitnessAssessment
    ) -> None:
        _assert_sealed(
            assessment, 'assessment_sha256', 'assessment_id'
        )
        existing = self.get_assessment(assessment.assessment_id)
        if existing is not None:
            if existing.assessment_sha256 == assessment.assessment_sha256:
                return
            raise CalibrationLifecycleConflictError(
                'fitness assessments are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_instrument_fitness_assessments (
                    assessment_id, assessment_sha256, document_id,
                    instrument_ref_id, at_utc, state,
                    evaluation_version, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment.assessment_id,
                    assessment.assessment_sha256,
                    assessment.document_id,
                    assessment.instrument_ref.ref_id,
                    assessment.at_utc,
                    assessment.state,
                    assessment.evaluation_version,
                    assessment.evaluated_at_utc,
                    assessment.model_dump_json(),
                ),
            )

    def get_assessment(
        self, assessment_id: str
    ) -> CadInstrumentFitnessAssessment | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_instrument_fitness_assessments '
                'WHERE assessment_id=?',
                (assessment_id,),
            ).fetchone()
        if row is None:
            return None
        assessment = CadInstrumentFitnessAssessment.model_validate_json(
            row['payload_json']
        )
        if (
            assessment.assessment_id != row['assessment_id']
            or assessment.assessment_sha256
            != row['assessment_sha256']
            or assessment.document_id != row['document_id']
            or assessment.instrument_ref.ref_id
            != row['instrument_ref_id']
            or assessment.at_utc != row['at_utc']
            or assessment.state != row['state']
            or assessment.evaluation_version
            != row['evaluation_version']
            or assessment.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise CalibrationLifecycleIntegrityError(
                'assessment row disagrees with payload'
            )
        return assessment

    def list_assessments(
        self,
        document_id: str | None = None,
        *,
        instrument_id: str | None = None,
    ) -> tuple[CadInstrumentFitnessAssessment, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if document_id is not None:
            clauses.append('document_id=?')
            params.append(document_id)
        if instrument_id is not None:
            clauses.append('instrument_ref_id=?')
            params.append(instrument_id)
        where = ' AND '.join(clauses) if clauses else '1=1'
        return self._list(
            table='cad_instrument_fitness_assessments',
            model=CadInstrumentFitnessAssessment,
            where=where,
            params=tuple(params),
        )

    # ------------------------------------------------------------------
    # Out-of-tolerance reviews

    def save_review(self, review: CadOutOfToleranceReview) -> None:
        _assert_sealed(review, 'review_sha256', 'review_id')
        existing = self.get_review(review.review_id)
        if existing is not None:
            if existing.review_sha256 == review.review_sha256:
                return
            raise CalibrationLifecycleConflictError(
                'out-of-tolerance reviews are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_out_of_tolerance_reviews (
                    review_id, review_sha256, document_id,
                    instrument_ref_id, triggering_ref_id,
                    last_known_valid_at_utc, recorded_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review.review_id,
                    review.review_sha256,
                    review.document_id,
                    review.instrument_ref.ref_id,
                    review.triggering_ref.ref_id,
                    review.last_known_valid_at_utc,
                    review.recorded_at_utc,
                    review.model_dump_json(),
                ),
            )

    def get_review(
        self, review_id: str
    ) -> CadOutOfToleranceReview | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_out_of_tolerance_reviews '
                'WHERE review_id=?',
                (review_id,),
            ).fetchone()
        if row is None:
            return None
        review = CadOutOfToleranceReview.model_validate_json(
            row['payload_json']
        )
        if (
            review.review_id != row['review_id']
            or review.review_sha256 != row['review_sha256']
            or review.document_id != row['document_id']
            or review.instrument_ref.ref_id != row['instrument_ref_id']
            or review.triggering_ref.ref_id
            != row['triggering_ref_id']
            or review.last_known_valid_at_utc
            != row['last_known_valid_at_utc']
            or review.recorded_at_utc != row['recorded_at_utc']
        ):
            raise CalibrationLifecycleIntegrityError(
                'review row disagrees with payload'
            )
        return review

    def list_reviews(
        self,
        document_id: str | None = None,
        *,
        instrument_id: str | None = None,
    ) -> tuple[CadOutOfToleranceReview, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if document_id is not None:
            clauses.append('document_id=?')
            params.append(document_id)
        if instrument_id is not None:
            clauses.append('instrument_ref_id=?')
            params.append(instrument_id)
        where = ' AND '.join(clauses) if clauses else '1=1'
        return self._list(
            table='cad_out_of_tolerance_reviews',
            model=CadOutOfToleranceReview,
            where=where,
            params=tuple(params),
        )


__all__ = [
    'CadCalibrationLifecycleRepository',
    'CalibrationLifecycleConflictError',
    'CalibrationLifecycleIntegrityError',
]
