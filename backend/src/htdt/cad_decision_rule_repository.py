"""Append-only persistence for decision rules and verdicts (#577).

Persisted rows are re-validated on read — the sealed semantic hash and the
indexed identity columns must agree with the payload, so a corrupted or
tampered row fails closed instead of silently authorizing a stale
recommendation. Records are immutable: saving the same identity twice with
identical payload is a no-op, with different payload is a
:class:`DecisionRuleConflictError`.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_decision_rule import DecisionRuleSpec, RecommendationEvidenceVerdict
from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .clock import utc_now_iso as _utc_now


class DecisionRuleConflictError(ValueError):
    """A decision rule/verdict was saved twice with different content."""


class CadDecisionRuleRepository:
    """Durable store for #577 decision rule specs and verdicts."""

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
                'cad_decision_rule_specs',
                'cad_decision_verdicts',
            )

    # -- decision rule specs -------------------------------------------------

    def save_rule(self, rule: DecisionRuleSpec) -> None:
        existing = self._select_row(
            'cad_decision_rule_specs', 'rule_id', rule.rule_id
        )
        payload = rule.model_dump_json()
        if existing is not None:
            if existing['payload_json'] != payload:
                raise DecisionRuleConflictError(
                    f'rule_id {rule.rule_id} is persisted with different content'
                )
            return
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_decision_rule_specs ('
                'rule_id, semantic_sha256, document_id, decision_type, '
                'criterion_id, payload_json, recorded_at_utc'
                ') VALUES (?, ?, ?, ?, ?, ?, ?)',
                (
                    rule.rule_id,
                    rule.semantic_sha256,
                    rule.document_id,
                    rule.decision_type,
                    rule.criterion_id,
                    payload,
                    _utc_now(),
                ),
            )

    def get_rule(self, rule_id: str) -> DecisionRuleSpec | None:
        row = self._select_row(
            'cad_decision_rule_specs', 'rule_id', rule_id
        )
        if row is None:
            return None
        return self._row_to_rule(row)

    def list_rules_for_document(
        self, document_id: str
    ) -> tuple[DecisionRuleSpec, ...]:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                'SELECT * FROM cad_decision_rule_specs '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(self._row_to_rule(row) for row in rows)

    def _row_to_rule(self, row: sqlite3.Row) -> DecisionRuleSpec:
        rule = DecisionRuleSpec.model_validate_json(row['payload_json'])
        if (
            row['rule_id'] != rule.rule_id
            or row['semantic_sha256'] != rule.semantic_sha256
            or row['document_id'] != rule.document_id
            or row['decision_type'] != rule.decision_type
            or row['criterion_id'] != rule.criterion_id
        ):
            raise ValueError(
                'persisted decision rule row disagrees with its payload'
            )
        return rule

    # -- evidence verdicts ---------------------------------------------------

    def save_verdict(self, verdict: RecommendationEvidenceVerdict) -> None:
        existing = self._select_row(
            'cad_decision_verdicts', 'verdict_id', verdict.verdict_id
        )
        payload = verdict.model_dump_json()
        if existing is not None:
            if existing['payload_json'] != payload:
                raise DecisionRuleConflictError(
                    f'verdict_id {verdict.verdict_id} is persisted with '
                    'different content'
                )
            return
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_decision_verdicts ('
                'verdict_id, semantic_sha256, document_id, rule_id, '
                'rule_sha256, decision_type, verdict, payload_json, '
                'recorded_at_utc'
                ') VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    verdict.verdict_id,
                    verdict.semantic_sha256,
                    verdict.document_id,
                    verdict.rule_id,
                    verdict.rule_sha256,
                    verdict.decision_type,
                    verdict.verdict,
                    payload,
                    _utc_now(),
                ),
            )

    def get_verdict(
        self, verdict_id: str
    ) -> RecommendationEvidenceVerdict | None:
        row = self._select_row(
            'cad_decision_verdicts', 'verdict_id', verdict_id
        )
        if row is None:
            return None
        return self._row_to_verdict(row)

    def list_verdicts_for_document(
        self, document_id: str
    ) -> tuple[RecommendationEvidenceVerdict, ...]:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                'SELECT * FROM cad_decision_verdicts '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(self._row_to_verdict(row) for row in rows)

    def list_verdicts_for_rule(
        self, rule_id: str
    ) -> tuple[RecommendationEvidenceVerdict, ...]:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                'SELECT * FROM cad_decision_verdicts '
                'WHERE rule_id=? ORDER BY seq ASC',
                (rule_id,),
            ).fetchall()
        return tuple(self._row_to_verdict(row) for row in rows)

    def _row_to_verdict(
        self, row: sqlite3.Row
    ) -> RecommendationEvidenceVerdict:
        verdict = RecommendationEvidenceVerdict.model_validate_json(
            row['payload_json']
        )
        if (
            row['verdict_id'] != verdict.verdict_id
            or row['semantic_sha256'] != verdict.semantic_sha256
            or row['document_id'] != verdict.document_id
            or row['rule_id'] != verdict.rule_id
            or row['rule_sha256'] != verdict.rule_sha256
            or row['decision_type'] != verdict.decision_type
            or row['verdict'] != verdict.verdict
        ):
            raise ValueError(
                'persisted decision verdict row disagrees with its payload'
            )
        return verdict

    # -- shared ---------------------------------------------------------------

    def _select_row(
        self, table: str, key_column: str, key: str
    ) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                f'SELECT * FROM {table} WHERE {key_column}=?',
                (key,),
            ).fetchone()


__all__ = [
    'CadDecisionRuleRepository',
    'DecisionRuleConflictError',
]
