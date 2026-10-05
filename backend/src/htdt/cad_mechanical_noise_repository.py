"""Append-only persistence for the mechanical-noise authority (#589).

Four tables:

* ``cad_mechanical_noise_tests`` — sealed ``MechanicalNoiseTest``
  stress runs keyed by ``test_id``.
* ``cad_rattle_events`` — sealed ``RattleEvent`` records; an event may
  only be persisted against a stored test whose sha matches.
* ``cad_remediation_actions`` — append-only fix records whose event
  refs must resolve to stored events.
* ``cad_mechanical_noise_qualifications`` — derived verdicts bound to
  stored tests.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_mechanical_noise import (
    MechanicalNoiseQualification,
    MechanicalNoiseTest,
    RattleEvent,
    RemediationAction,
)
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256


class MechanicalNoiseConflictError(ValueError):
    """A mechanical-noise save violated append-only identity rules."""


class MechanicalNoiseIntegrityError(ValueError):
    """A stored mechanical-noise row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha or getattr(record, id_field) != (
        getattr(record, id_field).split(':')[0] + ':' + sha
    ):
        raise MechanicalNoiseIntegrityError(
            'record payload does not match its sealed identity'
        )


class CadMechanicalNoiseRepository:
    """Native storage for rattle/structure-borne qualification."""

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
                'cad_mechanical_noise_tests',
                'cad_rattle_events',
                'cad_remediation_actions',
                'cad_mechanical_noise_qualifications',
            )

    # ------------------------------------------------------------------
    # Tests

    def save_test(self, test: MechanicalNoiseTest) -> None:
        _assert_sealed(test, 'test_sha256', 'test_id')
        existing = self.get_test(test.test_id)
        if existing is not None:
            if existing.test_sha256 == test.test_sha256:
                return
            raise MechanicalNoiseConflictError(
                'mechanical noise tests are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_mechanical_noise_tests (
                    test_id, test_sha256, document_id, label,
                    signal_type, captured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    test.test_id,
                    test.test_sha256,
                    test.document_id,
                    test.label,
                    test.stimulus.signal_type,
                    test.captured_at_utc,
                    test.model_dump_json(),
                ),
            )

    def get_test(self, test_id: str) -> MechanicalNoiseTest | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT test_id, test_sha256, document_id, label,
                       signal_type, captured_at_utc, payload_json
                FROM cad_mechanical_noise_tests
                WHERE test_id=?
                """,
                (test_id,),
            ).fetchone()
        if row is None:
            return None
        return self._test_from_row(row)

    def list_tests(
        self, document_id: str
    ) -> tuple[MechanicalNoiseTest, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT test_id, test_sha256, document_id, label,
                       signal_type, captured_at_utc, payload_json
                FROM cad_mechanical_noise_tests
                WHERE document_id=?
                ORDER BY captured_at_utc, test_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._test_from_row(row) for row in rows)

    def _test_from_row(self, row: sqlite3.Row) -> MechanicalNoiseTest:
        test = MechanicalNoiseTest.model_validate_json(row['payload_json'])
        if (
            test.test_id != row['test_id']
            or test.test_sha256 != row['test_sha256']
            or test.document_id != row['document_id']
            or test.label != row['label']
            or test.stimulus.signal_type != row['signal_type']
            or test.captured_at_utc != row['captured_at_utc']
        ):
            raise MechanicalNoiseIntegrityError(
                'mechanical noise test row disagrees with its payload'
            )
        return test

    # ------------------------------------------------------------------
    # Rattle events

    def save_event(self, event: RattleEvent) -> None:
        _assert_sealed(event, 'event_sha256', 'event_id')
        existing = self.get_event(event.event_id)
        if existing is not None:
            if existing.event_sha256 == event.event_sha256:
                return
            raise MechanicalNoiseConflictError(
                'rattle events are append-only'
            )
        test = self.get_test(event.test_id)
        if test is None:
            raise MechanicalNoiseIntegrityError(
                'a rattle event must reference a persisted test'
            )
        if test.test_sha256 != event.test_sha256:
            raise MechanicalNoiseIntegrityError(
                'rattle event test hash does not match the stored test'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_rattle_events (
                    event_id, event_sha256, document_id, test_id,
                    test_sha256, kind, localization_state,
                    detected_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.event_sha256,
                    event.document_id,
                    event.test_id,
                    event.test_sha256,
                    event.kind,
                    event.localization_state,
                    event.detected_at_utc,
                    event.model_dump_json(),
                ),
            )

    def get_event(self, event_id: str) -> RattleEvent | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT event_id, event_sha256, document_id, test_id,
                       test_sha256, kind, localization_state,
                       detected_at_utc, payload_json
                FROM cad_rattle_events
                WHERE event_id=?
                """,
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        return self._event_from_row(row)

    def events_for_test(
        self, test_id: str
    ) -> tuple[RattleEvent, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT event_id, event_sha256, document_id, test_id,
                       test_sha256, kind, localization_state,
                       detected_at_utc, payload_json
                FROM cad_rattle_events
                WHERE test_id=?
                ORDER BY detected_at_utc, event_id
                """,
                (test_id,),
            ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    def _event_from_row(self, row: sqlite3.Row) -> RattleEvent:
        event = RattleEvent.model_validate_json(row['payload_json'])
        if (
            event.event_id != row['event_id']
            or event.event_sha256 != row['event_sha256']
            or event.document_id != row['document_id']
            or event.test_id != row['test_id']
            or event.test_sha256 != row['test_sha256']
            or event.kind != row['kind']
            or event.localization_state != row['localization_state']
            or event.detected_at_utc != row['detected_at_utc']
        ):
            raise MechanicalNoiseIntegrityError(
                'rattle event row disagrees with its payload'
            )
        return event

    # ------------------------------------------------------------------
    # Remediation actions

    def save_remediation(self, action: RemediationAction) -> None:
        _assert_sealed(action, 'action_sha256', 'action_id')
        existing = self.get_remediation(action.action_id)
        if existing is not None:
            if existing.action_sha256 == action.action_sha256:
                return
            raise MechanicalNoiseConflictError(
                'remediation actions are append-only'
            )
        for event_id, event_sha in action.event_refs:
            stored = self.get_event(event_id)
            if stored is None:
                raise MechanicalNoiseIntegrityError(
                    'a remediation must reference a persisted event'
                )
            if stored.event_sha256 != event_sha:
                raise MechanicalNoiseIntegrityError(
                    'remediation event hash does not match the stored '
                    'event'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_remediation_actions (
                    action_id, action_sha256, document_id,
                    action_kind, performed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    action.action_id,
                    action.action_sha256,
                    action.document_id,
                    action.action_kind,
                    action.performed_at_utc,
                    action.model_dump_json(),
                ),
            )

    def get_remediation(
        self, action_id: str
    ) -> RemediationAction | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT action_id, action_sha256, document_id,
                       action_kind, performed_at_utc, payload_json
                FROM cad_remediation_actions
                WHERE action_id=?
                """,
                (action_id,),
            ).fetchone()
        if row is None:
            return None
        action = RemediationAction.model_validate_json(row['payload_json'])
        if (
            action.action_id != row['action_id']
            or action.action_sha256 != row['action_sha256']
            or action.document_id != row['document_id']
            or action.action_kind != row['action_kind']
            or action.performed_at_utc != row['performed_at_utc']
        ):
            raise MechanicalNoiseIntegrityError(
                'remediation row disagrees with its payload'
            )
        return action

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: MechanicalNoiseQualification
    ) -> None:
        _assert_sealed(
            qualification, 'qualification_sha256', 'qualification_id'
        )
        existing = self.get_qualification(qualification.qualification_id)
        if existing is not None:
            if existing.qualification_sha256 == qualification.qualification_sha256:
                return
            raise MechanicalNoiseConflictError(
                'mechanical noise qualifications are append-only'
            )
        for test_id in qualification.test_ids:
            if self.get_test(test_id) is None:
                raise MechanicalNoiseIntegrityError(
                    'a qualification must reference persisted tests'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_mechanical_noise_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    overall_verdict, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.overall_verdict,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> MechanicalNoiseQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT qualification_id, qualification_sha256, document_id,
                       overall_verdict, evaluated_at_utc, payload_json
                FROM cad_mechanical_noise_qualifications
                WHERE qualification_id=?
                """,
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = MechanicalNoiseQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.overall_verdict != row['overall_verdict']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise MechanicalNoiseIntegrityError(
                'mechanical noise qualification row disagrees with its '
                'payload'
            )
        return qualification


__all__ = [
    'CadMechanicalNoiseRepository',
    'MechanicalNoiseConflictError',
    'MechanicalNoiseIntegrityError',
]
