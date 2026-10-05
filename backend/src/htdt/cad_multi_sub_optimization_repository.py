"""Append-only persistence for conventional multi-sub optimization (#569).

Candidates, seat-population evaluations, qualifications, stage comparisons
and deployment verifications are durable project records stored by exact
semantic identity. Persisted rows are re-verified on read — every sealed
hash is recomputed — so a row whose payload no longer matches its declared
verdict fails closed instead of authorizing a stale multi-seat claim.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_multi_sub_optimization import (
    MultiSubCandidate,
    MultiSubDeploymentVerification,
    MultiSubEvaluation,
    MultiSubQualification,
    MultiSubStageComparison,
)
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite
from .clock import utc_now_iso as _utc_now


class MultiSubConflictError(ValueError):
    """A multi-sub authority was saved twice with different content."""


class CadMultiSubOptimizationRepository:
    """Durable store for #569 multi-sub qualification evidence."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_multi_sub_candidates',
                'cad_multi_sub_evaluations',
                'cad_multi_sub_qualifications',
                'cad_multi_sub_stage_comparisons',
                'cad_multi_sub_deployments',
            )

    # -- candidates ------------------------------------------------------

    def save_candidate(self, candidate: MultiSubCandidate) -> None:
        revision = self.scene_repository.get(candidate.scene_revision_id)
        if (
            revision is None
            or revision.document_id != candidate.document_id
            or revision.content_hash != candidate.scene_content_hash
        ):
            raise ValueError(
                'candidate pins a scene revision that is not persisted'
            )
        self._insert_once(
            table='cad_multi_sub_candidates',
            key_column='candidate_id',
            key=candidate.candidate_id,
            columns=(
                'candidate_id',
                'candidate_sha256',
                'document_id',
                'scene_revision_id',
                'strategy',
                'payload_json',
                'recorded_at_utc',
            ),
            values=(
                candidate.candidate_id,
                candidate.candidate_sha256,
                candidate.document_id,
                candidate.scene_revision_id,
                candidate.strategy,
                candidate.model_dump_json(),
                _utc_now(),
            ),
            payload=candidate.model_dump_json(),
        )

    def get_candidate(self, candidate_id: str) -> MultiSubCandidate | None:
        row = self._select_row(
            'cad_multi_sub_candidates', 'candidate_id', candidate_id
        )
        if row is None:
            return None
        candidate = MultiSubCandidate.model_validate_json(row['payload_json'])
        if (
            row['candidate_sha256'] != candidate.candidate_sha256
            or row['document_id'] != candidate.document_id
            or row['scene_revision_id'] != candidate.scene_revision_id
            or row['strategy'] != candidate.strategy
        ):
            raise ValueError('persisted candidate row disagrees with its payload')
        return candidate

    def list_candidates(
        self, document_id: str
    ) -> tuple[MultiSubCandidate, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_multi_sub_candidates '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            MultiSubCandidate.model_validate_json(row['payload_json'])
            for row in rows
        )

    # -- evaluations ------------------------------------------------------

    def save_evaluation(self, evaluation: MultiSubEvaluation) -> None:
        candidate = self.get_candidate(evaluation.candidate_id)
        if candidate is None or (
            candidate.candidate_sha256 != evaluation.candidate_sha256
        ):
            raise ValueError(
                'evaluation pins a candidate that is not persisted'
            )
        declared = candidate.partition.seats_for(evaluation.population)
        bound = tuple(
            binding.seat_entity_id for binding in evaluation.seat_bindings
        )
        if bound != declared:
            raise ValueError(
                'evaluation seat bindings do not match the persisted '
                'candidate partition'
            )
        self._insert_once(
            table='cad_multi_sub_evaluations',
            key_column='evaluation_id',
            key=evaluation.evaluation_id,
            columns=(
                'evaluation_id',
                'evaluation_sha256',
                'candidate_id',
                'document_id',
                'population',
                'payload_json',
                'recorded_at_utc',
            ),
            values=(
                evaluation.evaluation_id,
                evaluation.evaluation_sha256,
                evaluation.candidate_id,
                evaluation.document_id,
                evaluation.population,
                evaluation.model_dump_json(),
                _utc_now(),
            ),
            payload=evaluation.model_dump_json(),
        )

    def get_evaluation(
        self, evaluation_id: str
    ) -> MultiSubEvaluation | None:
        row = self._select_row(
            'cad_multi_sub_evaluations', 'evaluation_id', evaluation_id
        )
        if row is None:
            return None
        evaluation = MultiSubEvaluation.model_validate_json(row['payload_json'])
        if (
            row['evaluation_sha256'] != evaluation.evaluation_sha256
            or row['candidate_id'] != evaluation.candidate_id
            or row['document_id'] != evaluation.document_id
            or row['population'] != evaluation.population
        ):
            raise ValueError(
                'persisted evaluation row disagrees with its payload'
            )
        return evaluation

    def list_evaluations(
        self, candidate_id: str
    ) -> tuple[MultiSubEvaluation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_multi_sub_evaluations '
                'WHERE candidate_id=? ORDER BY seq ASC',
                (candidate_id,),
            ).fetchall()
        return tuple(
            MultiSubEvaluation.model_validate_json(row['payload_json'])
            for row in rows
        )

    def list_evaluations_for_document(
        self, document_id: str
    ) -> tuple[MultiSubEvaluation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_multi_sub_evaluations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            MultiSubEvaluation.model_validate_json(row['payload_json'])
            for row in rows
        )

    # -- qualifications ----------------------------------------------------

    def save_qualification(
        self, qualification: MultiSubQualification
    ) -> None:
        for candidate_id, candidate_sha in (
            (
                qualification.baseline_candidate_id,
                qualification.baseline_candidate_sha256,
            ),
            (qualification.candidate_id, qualification.candidate_sha256),
        ):
            candidate = self.get_candidate(candidate_id)
            if candidate is None or candidate.candidate_sha256 != candidate_sha:
                raise ValueError(
                    'qualification pins a candidate that is not persisted'
                )
        for evaluation_id, evaluation_sha in (
            (
                qualification.baseline_optimization_evaluation_id,
                qualification.baseline_optimization_evaluation_sha256,
            ),
            (
                qualification.candidate_optimization_evaluation_id,
                qualification.candidate_optimization_evaluation_sha256,
            ),
            (
                qualification.baseline_holdout_evaluation_id,
                qualification.baseline_holdout_evaluation_sha256,
            ),
            (
                qualification.candidate_holdout_evaluation_id,
                qualification.candidate_holdout_evaluation_sha256,
            ),
        ):
            if evaluation_id is None:
                continue
            evaluation = self.get_evaluation(evaluation_id)
            if evaluation is None or evaluation.evaluation_sha256 != evaluation_sha:
                raise ValueError(
                    'qualification pins an evaluation that is not persisted'
                )
        self._insert_once(
            table='cad_multi_sub_qualifications',
            key_column='qualification_id',
            key=qualification.qualification_id,
            columns=(
                'qualification_id',
                'qualification_sha256',
                'document_id',
                'baseline_candidate_id',
                'candidate_id',
                'claim',
                'verdict',
                'payload_json',
                'recorded_at_utc',
            ),
            values=(
                qualification.qualification_id,
                qualification.qualification_sha256,
                qualification.document_id,
                qualification.baseline_candidate_id,
                qualification.candidate_id,
                qualification.claim,
                qualification.verdict,
                qualification.model_dump_json(),
                _utc_now(),
            ),
            payload=qualification.model_dump_json(),
        )

    def get_qualification(
        self, qualification_id: str
    ) -> MultiSubQualification | None:
        row = self._select_row(
            'cad_multi_sub_qualifications', 'qualification_id', qualification_id
        )
        if row is None:
            return None
        qualification = MultiSubQualification.model_validate_json(
            row['payload_json']
        )
        if (
            row['qualification_sha256'] != qualification.qualification_sha256
            or row['baseline_candidate_id'] != qualification.baseline_candidate_id
            or row['candidate_id'] != qualification.candidate_id
            or row['claim'] != qualification.claim
            or row['verdict'] != qualification.verdict
        ):
            raise ValueError(
                'persisted qualification row disagrees with its payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str
    ) -> tuple[MultiSubQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_multi_sub_qualifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            MultiSubQualification.model_validate_json(row['payload_json'])
            for row in rows
        )

    # -- stage comparisons ---------------------------------------------------

    def save_stage_comparison(
        self, comparison: MultiSubStageComparison
    ) -> None:
        for entry in comparison.entries:
            candidate = self.get_candidate(entry.candidate_id)
            if candidate is None or (
                candidate.candidate_sha256 != entry.candidate_sha256
            ):
                raise ValueError(
                    'stage comparison pins a candidate that is not persisted'
                )
            expected = candidate.strategy
            from .cad_multi_sub_optimization import STRATEGY_TO_STAGE

            if STRATEGY_TO_STAGE[expected] != entry.stage:
                raise ValueError(
                    f'stage {entry.stage} does not match candidate strategy '
                    f'{expected}'
                )
            evaluation = self.get_evaluation(entry.evaluation_id)
            if evaluation is None or (
                evaluation.evaluation_sha256 != entry.evaluation_sha256
            ):
                raise ValueError(
                    'stage comparison pins an evaluation that is not persisted'
                )
        self._insert_once(
            table='cad_multi_sub_stage_comparisons',
            key_column='comparison_id',
            key=comparison.comparison_id,
            columns=(
                'comparison_id',
                'comparison_sha256',
                'document_id',
                'payload_json',
                'recorded_at_utc',
            ),
            values=(
                comparison.comparison_id,
                comparison.comparison_sha256,
                comparison.document_id,
                comparison.model_dump_json(),
                _utc_now(),
            ),
            payload=comparison.model_dump_json(),
        )

    def get_stage_comparison(
        self, comparison_id: str
    ) -> MultiSubStageComparison | None:
        row = self._select_row(
            'cad_multi_sub_stage_comparisons', 'comparison_id', comparison_id
        )
        if row is None:
            return None
        comparison = MultiSubStageComparison.model_validate_json(
            row['payload_json']
        )
        if (
            row['comparison_sha256'] != comparison.comparison_sha256
            or row['document_id'] != comparison.document_id
        ):
            raise ValueError(
                'persisted stage comparison row disagrees with its payload'
            )
        return comparison

    def list_stage_comparisons(
        self, document_id: str
    ) -> tuple[MultiSubStageComparison, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_multi_sub_stage_comparisons '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            MultiSubStageComparison.model_validate_json(row['payload_json'])
            for row in rows
        )

    # -- deployment verifications ---------------------------------------------

    def save_deployment(
        self, verification: MultiSubDeploymentVerification
    ) -> None:
        candidate = self.get_candidate(verification.candidate_id)
        if candidate is None or (
            candidate.candidate_sha256 != verification.candidate_sha256
        ):
            raise ValueError(
                'deployment verification pins a candidate that is not persisted'
            )
        if verification.qualification_id != 'none':
            qualification = self.get_qualification(
                verification.qualification_id
            )
            if qualification is None or (
                qualification.qualification_sha256
                != verification.qualification_sha256
            ):
                raise ValueError(
                    'deployment verification pins a qualification that is '
                    'not persisted'
                )
        self._insert_once(
            table='cad_multi_sub_deployments',
            key_column='verification_id',
            key=verification.verification_id,
            columns=(
                'verification_id',
                'verification_sha256',
                'qualification_id',
                'candidate_id',
                'document_id',
                'verdict',
                'payload_json',
                'recorded_at_utc',
            ),
            values=(
                verification.verification_id,
                verification.verification_sha256,
                verification.qualification_id,
                verification.candidate_id,
                verification.document_id,
                verification.verdict,
                verification.model_dump_json(),
                _utc_now(),
            ),
            payload=verification.model_dump_json(),
        )

    def get_deployment(
        self, verification_id: str
    ) -> MultiSubDeploymentVerification | None:
        row = self._select_row(
            'cad_multi_sub_deployments', 'verification_id', verification_id
        )
        if row is None:
            return None
        verification = MultiSubDeploymentVerification.model_validate_json(
            row['payload_json']
        )
        if (
            row['verification_sha256'] != verification.verification_sha256
            or row['candidate_id'] != verification.candidate_id
            or row['document_id'] != verification.document_id
            or row['verdict'] != verification.verdict
        ):
            raise ValueError(
                'persisted deployment row disagrees with its payload'
            )
        return verification

    def list_deployments(
        self, document_id: str
    ) -> tuple[MultiSubDeploymentVerification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_multi_sub_deployments '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            MultiSubDeploymentVerification.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # -- low-level helpers ----------------------------------------------------

    def _select(
        self, table: str, key_column: str, key: str
    ) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                f'SELECT payload_json FROM {table} WHERE {key_column}=?',
                (key,),
            ).fetchone()
        return None if row is None else str(row[0])

    def _select_row(
        self, table: str, key_column: str, key: str
    ) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                f'SELECT * FROM {table} WHERE {key_column}=?',
                (key,),
            ).fetchone()

    def _insert_once(
        self,
        *,
        table: str,
        key_column: str,
        key: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        payload: str,
    ) -> None:
        existing = self._select(table, key_column, key)
        if existing is not None:
            if existing != payload:
                raise MultiSubConflictError(
                    f'{key_column} {key} is persisted with different content'
                )
            return
        placeholders = ', '.join('?' for _ in values)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f'INSERT INTO {table} ({", ".join(columns)}) '
                f'VALUES ({placeholders})',
                values,
            )
