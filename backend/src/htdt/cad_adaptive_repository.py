from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from .cad_adaptive_planner import (
    ADAPTIVE_ALGORITHM_VERSION,
    CadAdaptivePlan,
    build_adaptive_plan,
    development_validation_ready,
    production_validation_ready,
    select_predicted_evaluations,
)
from .cad_model_validation_repository import CadModelValidationRepository
from .cad_objective_repository import CadObjectiveRepository
from .cad_search import iter_cad_candidate_pages
from .cad_search_repository import CadSearchRepository


class CadAdaptivePlanRepository:
    """Immutable O70 adaptive-plan storage bound to exact O10/O30/O60 authority."""

    def __init__(
        self,
        search_repository: CadSearchRepository,
        validation_repository: CadModelValidationRepository,
        objective_repository: CadObjectiveRepository,
    ) -> None:
        self.search_repository = search_repository
        self.validation_repository = validation_repository
        self.objective_repository = objective_repository
        self.path = Path(search_repository.path)
        if (
            Path(validation_repository.path) != self.path
            or Path(objective_repository.path) != self.path
        ):
            raise ValueError('adaptive repositories must share one native CAD database')
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        # Kept idempotent so opening an already-migrated database is harmless.
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cad_adaptive_plans (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    search_spec_id TEXT NOT NULL,
                    validation_id TEXT NOT NULL,
                    execution_scope TEXT NOT NULL,
                    selected_candidate_id TEXT NOT NULL,
                    adaptive_sha256 TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    FOREIGN KEY(search_spec_id) REFERENCES cad_search_specs(search_spec_id)
                );
                CREATE INDEX IF NOT EXISTS idx_adaptive_search_seq
                    ON cad_adaptive_plans(search_spec_id, seq ASC);
                """
            )

    def _all_candidates(self, spec):
        candidates = []
        candidate_set_sha256: str | None = None
        for page in iter_cad_candidate_pages(
            self.search_repository.scene_repository,
            spec,
        ):
            candidate_set_sha256 = page.candidate_set_sha256
            candidates.extend(page.candidates)
        if candidate_set_sha256 is None or not candidates:
            raise ValueError('adaptive SearchSpec has no feasible candidates')
        return tuple(candidates), candidate_set_sha256

    def _require_candidate_authority(self, plan: CadAdaptivePlan, spec):
        """Regenerate the full canonical candidate set and require membership.

        The persisted ``candidate_set_sha256`` must still equal the set the
        SearchSpec regenerates today, and every candidate the plan references
        — proposals, calibration training rows, measured exclusions, and
        bound predicted-objective evidence — must be a member of that exact
        set. Returns the resolved candidates and their set identity so the
        caller can replay the planner over the identical enumeration.
        """

        candidates, candidate_set_sha256 = self._all_candidates(spec)
        if candidate_set_sha256 != plan.candidate_set_sha256:
            raise ValueError(
                'adaptive candidate-set hash no longer matches SearchSpec'
            )
        candidate_ids = {candidate.candidate_id for candidate in candidates}
        referenced = (
            {proposal.candidate_id for proposal in plan.proposals}
            | set(plan.training_candidate_ids)
            | set(plan.excluded_measured_candidate_ids)
            | {ref.candidate_id for ref in plan.predicted_evaluation_refs}
        )
        missing = referenced - candidate_ids
        if missing:
            raise ValueError(
                'adaptive plan references candidates outside SearchSpec: '
                + ', '.join(sorted(missing))
            )
        return candidates, candidate_set_sha256

    def _require_plan_authority(self, plan: CadAdaptivePlan) -> None:
        """Replay the exact O10/O30/O60 authority one plan binds to.

        Revalidates the SearchSpec binding, the referenced O60
        ValidationRecord and its current scope gate, and the regenerated
        candidate set; resolves the current canonical predicted O30
        evaluations through the objective read path and requires every
        identity-bound plan ref to still be that exact evidence; then
        reruns build_adaptive_plan over the persisted algorithm inputs and
        requires the submitted plan to be that exact canonical output.
        Only the pinned ADAPTIVE_ALGORITHM_VERSION plan shape can be
        constructed or replayed — any other algorithm version is rejected
        rather than reinterpreted. Superseded, stale, or caller-fabricated
        evidence fails closed. Shared by save-time validation and every
        authoritative plan read.
        """
        if plan.algorithm_version != ADAPTIVE_ALGORITHM_VERSION:
            raise ValueError('adaptive plan algorithm version is not replayable')

        spec = self.search_repository.get(plan.search_spec_id)
        if spec is None:
            raise ValueError('adaptive SearchSpec does not exist')
        if (
            spec.document_id != plan.document_id
            or spec.search_spec_sha256 != plan.search_spec_sha256
        ):
            raise ValueError('adaptive SearchSpec authority mismatch')

        validation = self.validation_repository.get(plan.validation_id)
        if validation is None:
            raise ValueError('adaptive O60 ValidationRecord does not exist')
        if validation.validation_sha256 != plan.validation_sha256:
            raise ValueError('adaptive O60 ValidationRecord hash mismatch')
        if (
            validation.search_spec_id != plan.search_spec_id
            or validation.search_spec_sha256 != plan.search_spec_sha256
            or validation.candidate_set_sha256 != plan.candidate_set_sha256
        ):
            raise ValueError('adaptive O60/SearchSpec authority mismatch')

        if plan.execution_scope == 'development_synthetic':
            if not development_validation_ready(validation):
                raise ValueError(
                    'adaptive synthetic development gate is no longer satisfied'
                )
        else:
            if not production_validation_ready(validation):
                raise ValueError('adaptive production O60 gate is not satisfied')
            current = self.validation_repository.latest_eligible_for_search_spec(
                plan.search_spec_id
            )
            if current is None or current.validation_id != validation.validation_id:
                raise ValueError(
                    'adaptive production plan must use the current O70-entry validation'
                )

        candidates, candidate_set_sha256 = self._require_candidate_authority(
            plan,
            spec,
        )

        objective_ids = tuple(
            dict.fromkeys(
                sample.objective_id for sample in validation.objective_samples
            )
        )
        predicted_evaluations = select_predicted_evaluations(
            self.objective_repository.list_evaluations(plan.search_spec_id),
            objective_ids,
        )
        current_evaluations = {
            evaluation.evaluation_sha256: evaluation
            for evaluation in predicted_evaluations
        }
        for ref in plan.predicted_evaluation_refs:
            evaluation = current_evaluations.get(ref.evaluation_sha256)
            if (
                evaluation is None
                or evaluation.evaluation_id != ref.evaluation_id
                or evaluation.candidate_id != ref.candidate_id
                or evaluation.search_spec_sha256 != spec.search_spec_sha256
            ):
                raise ValueError(
                    'adaptive plan predicted objective evidence is not current'
                )

        expected = build_adaptive_plan(
            spec=spec,
            candidate_set_sha256=candidate_set_sha256,
            validation=validation,
            candidates=candidates,
            predicted_evaluations=predicted_evaluations,
            execution_scope=plan.execution_scope,
            length_scale_m=plan.length_scale_m,
            proposal_limit=plan.proposal_limit,
        )
        if expected.adaptive_sha256 != plan.adaptive_sha256:
            raise ValueError(
                'adaptive plan does not match canonical planner replay'
            )

    def save(self, plan: CadAdaptivePlan) -> None:
        if not isinstance(plan, CadAdaptivePlan):
            raise TypeError('plan must be CadAdaptivePlan')
        plan = CadAdaptivePlan.model_validate(plan.model_dump(mode='python'))
        # The authority replay resolves SearchSpec/O30/O60 rows through nested
        # repository connections, so it must finish BEFORE the write
        # transaction begins: opening them while BEGIN IMMEDIATE is held can
        # deadlock the write.
        self._require_plan_authority(plan)

        with closing(self._connect()) as connection, connection:
            # BEGIN IMMEDIATE holds the write lock so the duplicate recheck and
            # the insert are serialized: a racing writer cannot persist a second
            # row for the same plan identity or the same canonical plan hash.
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(
                'SELECT payload_json FROM cad_adaptive_plans WHERE plan_id=?',
                (plan.plan_id,),
            ).fetchone()
            if row is not None:
                persisted = CadAdaptivePlan.model_validate_json(
                    row['payload_json']
                )
                if persisted != plan:
                    raise ValueError(
                        'adaptive plan identity already exists '
                        'with different semantics'
                    )
                return
            row = connection.execute(
                'SELECT plan_id FROM cad_adaptive_plans WHERE adaptive_sha256=?',
                (plan.adaptive_sha256,),
            ).fetchone()
            if row is not None:
                raise ValueError(
                    'adaptive plan hash already persisted under another identity'
                )
            connection.execute(
                """
                INSERT INTO cad_adaptive_plans(
                    plan_id, document_id, search_spec_id, validation_id,
                    execution_scope, selected_candidate_id, adaptive_sha256,
                    payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.document_id,
                    plan.search_spec_id,
                    plan.validation_id,
                    plan.execution_scope,
                    plan.selected_candidate_id,
                    plan.adaptive_sha256,
                    plan.model_dump_json(),
                    plan.created_at_utc,
                ),
            )

    def _validated_plan(self, row: sqlite3.Row) -> CadAdaptivePlan:
        """Deserialize one persisted plan row and replay its exact authority."""

        plan = CadAdaptivePlan.model_validate_json(row['payload_json'])
        if (
            row['plan_id'] != plan.plan_id
            or row['document_id'] != plan.document_id
            or row['search_spec_id'] != plan.search_spec_id
            or row['validation_id'] != plan.validation_id
            or row['execution_scope'] != plan.execution_scope
            or row['selected_candidate_id'] != plan.selected_candidate_id
            or row['adaptive_sha256'] != plan.adaptive_sha256
            or row['created_at_utc'] != plan.created_at_utc
        ):
            raise ValueError(
                'persisted adaptive plan row disagrees with its payload'
            )
        self._require_plan_authority(plan)
        return plan

    def get(self, plan_id: str) -> CadAdaptivePlan | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_adaptive_plans WHERE plan_id=?',
                (plan_id,),
            ).fetchone()
        return None if row is None else self._validated_plan(row)

    def find_by_sha(
        self,
        search_spec_id: str,
        adaptive_sha256: str,
    ) -> CadAdaptivePlan | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_adaptive_plans '
                'WHERE search_spec_id=? AND adaptive_sha256=? '
                'ORDER BY seq DESC LIMIT 1',
                (search_spec_id, adaptive_sha256),
            ).fetchone()
        return None if row is None else self._validated_plan(row)

    def list_for_search_spec(self, search_spec_id: str) -> tuple[CadAdaptivePlan, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM cad_adaptive_plans '
                'WHERE search_spec_id=? ORDER BY seq ASC',
                (search_spec_id,),
            ).fetchall()
        return tuple(self._validated_plan(row) for row in rows)
