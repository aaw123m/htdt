"""Append-only persistence for the residual diagnostic-hypothesis
authority (#719, REV59-UNITS).

Four tables:

* ``cad_diagnostic_cases`` — sealed diagnostic cases bound to a
  residual/observation symptom.
* ``cad_diagnostic_hypotheses`` — sealed candidate explanations.
* ``cad_diagnostic_tests`` — sealed discrimination tests / controlled
  interventions.
* ``cad_diagnostic_verdicts`` — sealed case-level verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_diagnostic_hypothesis import (
    CadDiagnosticCase,
    CadDiagnosticHypothesis,
    CadDiagnosticTest,
    CadDiagnosticVerdict,
)


class DiagnosticConflictError(ValueError):
    """A diagnostic save violated append-only identity rules."""


class DiagnosticIntegrityError(ValueError):
    """A stored diagnostic row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise DiagnosticIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise DiagnosticIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadDiagnosticHypothesisRepository:
    """Native storage for the #719 diagnostic-hypothesis authority."""

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
                'cad_diagnostic_cases',
                'cad_diagnostic_hypotheses',
                'cad_diagnostic_tests',
                'cad_diagnostic_verdicts',
            )

    # ------------------------------------------------------------------
    # Cases

    def save_case(self, case: CadDiagnosticCase) -> None:
        _assert_sealed(case, 'case_sha256', 'case_id')
        existing = self.get_case(case.case_id)
        if existing is not None:
            if existing.case_sha256 == case.case_sha256:
                return
            raise DiagnosticConflictError(
                'diagnostic cases are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_diagnostic_cases (
                    case_id, case_sha256, document_id,
                    symptom_ref_id, status, opened_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case.case_id,
                    case.case_sha256,
                    case.document_id,
                    case.symptom_ref.ref_id,
                    case.status,
                    case.opened_at_utc,
                    case.model_dump_json(),
                ),
            )

    def get_case(self, case_id: str) -> CadDiagnosticCase | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_diagnostic_cases WHERE case_id=?',
                (case_id,),
            ).fetchone()
        if row is None:
            return None
        case = CadDiagnosticCase.model_validate_json(
            row['payload_json']
        )
        if (
            case.case_id != row['case_id']
            or case.case_sha256 != row['case_sha256']
            or case.document_id != row['document_id']
            or case.symptom_ref.ref_id != row['symptom_ref_id']
            or case.status != row['status']
            or case.opened_at_utc != row['opened_at_utc']
        ):
            raise DiagnosticIntegrityError(
                'diagnostic case row disagrees with payload'
            )
        return case

    def list_cases(
        self, document_id: str
    ) -> tuple[CadDiagnosticCase, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_diagnostic_cases '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadDiagnosticCase.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Hypotheses

    def save_hypothesis(
        self, hypothesis: CadDiagnosticHypothesis
    ) -> None:
        _assert_sealed(
            hypothesis, 'hypothesis_sha256', 'hypothesis_id'
        )
        existing = self.get_hypothesis(hypothesis.hypothesis_id)
        if existing is not None:
            if (
                existing.hypothesis_sha256
                == hypothesis.hypothesis_sha256
            ):
                return
            raise DiagnosticConflictError(
                'diagnostic hypotheses are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_diagnostic_hypotheses (
                    hypothesis_id, hypothesis_sha256, document_id,
                    case_ref_id, cause_family, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    hypothesis.hypothesis_id,
                    hypothesis.hypothesis_sha256,
                    hypothesis.document_id,
                    hypothesis.case_ref.ref_id,
                    hypothesis.cause_family,
                    hypothesis.declared_at_utc,
                    hypothesis.model_dump_json(),
                ),
            )

    def get_hypothesis(
        self, hypothesis_id: str
    ) -> CadDiagnosticHypothesis | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_diagnostic_hypotheses '
                'WHERE hypothesis_id=?',
                (hypothesis_id,),
            ).fetchone()
        if row is None:
            return None
        hypothesis = CadDiagnosticHypothesis.model_validate_json(
            row['payload_json']
        )
        if (
            hypothesis.hypothesis_id != row['hypothesis_id']
            or hypothesis.hypothesis_sha256 != row['hypothesis_sha256']
            or hypothesis.document_id != row['document_id']
            or hypothesis.case_ref.ref_id != row['case_ref_id']
            or hypothesis.cause_family != row['cause_family']
            or hypothesis.declared_at_utc != row['declared_at_utc']
        ):
            raise DiagnosticIntegrityError(
                'diagnostic hypothesis row disagrees with payload'
            )
        return hypothesis

    def list_hypotheses(
        self, document_id: str
    ) -> tuple[CadDiagnosticHypothesis, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_diagnostic_hypotheses '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadDiagnosticHypothesis.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Tests

    def save_test(self, test: CadDiagnosticTest) -> None:
        _assert_sealed(test, 'test_sha256', 'test_id')
        existing = self.get_test(test.test_id)
        if existing is not None:
            if existing.test_sha256 == test.test_sha256:
                return
            raise DiagnosticConflictError(
                'diagnostic tests are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_diagnostic_tests (
                    test_id, test_sha256, document_id,
                    case_ref_id, test_kind, verdict, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    test.test_id,
                    test.test_sha256,
                    test.document_id,
                    test.case_ref.ref_id,
                    test.test_kind,
                    test.verdict,
                    test.declared_at_utc,
                    test.model_dump_json(),
                ),
            )

    def get_test(self, test_id: str) -> CadDiagnosticTest | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_diagnostic_tests WHERE test_id=?',
                (test_id,),
            ).fetchone()
        if row is None:
            return None
        test = CadDiagnosticTest.model_validate_json(
            row['payload_json']
        )
        if (
            test.test_id != row['test_id']
            or test.test_sha256 != row['test_sha256']
            or test.document_id != row['document_id']
            or test.case_ref.ref_id != row['case_ref_id']
            or test.test_kind != row['test_kind']
            or test.verdict != row['verdict']
            or test.declared_at_utc != row['declared_at_utc']
        ):
            raise DiagnosticIntegrityError(
                'diagnostic test row disagrees with payload'
            )
        return test

    def list_tests(
        self, document_id: str
    ) -> tuple[CadDiagnosticTest, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_diagnostic_tests '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadDiagnosticTest.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Verdicts

    def save_verdict(self, verdict: CadDiagnosticVerdict) -> None:
        _assert_sealed(verdict, 'verdict_sha256', 'verdict_id')
        existing = self.get_verdict(verdict.verdict_id)
        if existing is not None:
            if existing.verdict_sha256 == verdict.verdict_sha256:
                return
            raise DiagnosticConflictError(
                'diagnostic verdicts are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_diagnostic_verdicts (
                    verdict_id, verdict_sha256, document_id,
                    case_ref_id, verdict, evaluation_version,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    verdict.verdict_id,
                    verdict.verdict_sha256,
                    verdict.document_id,
                    verdict.case_ref.ref_id,
                    verdict.verdict,
                    verdict.evaluation_version,
                    verdict.evaluated_at_utc,
                    verdict.model_dump_json(),
                ),
            )

    def get_verdict(
        self, verdict_id: str
    ) -> CadDiagnosticVerdict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_diagnostic_verdicts '
                'WHERE verdict_id=?',
                (verdict_id,),
            ).fetchone()
        if row is None:
            return None
        verdict = CadDiagnosticVerdict.model_validate_json(
            row['payload_json']
        )
        if (
            verdict.verdict_id != row['verdict_id']
            or verdict.verdict_sha256 != row['verdict_sha256']
            or verdict.document_id != row['document_id']
            or verdict.case_ref.ref_id != row['case_ref_id']
            or verdict.verdict != row['verdict']
            or verdict.evaluation_version != row['evaluation_version']
            or verdict.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise DiagnosticIntegrityError(
                'diagnostic verdict row disagrees with payload'
            )
        return verdict

    def list_verdicts(
        self, document_id: str
    ) -> tuple[CadDiagnosticVerdict, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_diagnostic_verdicts '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadDiagnosticVerdict.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadDiagnosticHypothesisRepository',
    'DiagnosticConflictError',
    'DiagnosticIntegrityError',
]
