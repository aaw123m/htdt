from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from .cad_adaptive_planner import (
    CadAdaptivePlan,
    development_validation_ready,
    production_validation_ready,
)
from .cad_model_validation_repository import CadModelValidationRepository
from .cad_search import iter_cad_candidate_pages
from .cad_search_repository import CadSearchRepository


class CadAdaptivePlanRepository:
    """Immutable O70 adaptive-plan storage bound to exact O10/O60 authority."""

    def __init__(
        self,
        search_repository: CadSearchRepository,
        validation_repository: CadModelValidationRepository,
    ) -> None:
        self.search_repository = search_repository
        self.validation_repository = validation_repository
        self.path = Path(search_repository.path)
        if Path(validation_repository.path) != self.path:
            raise ValueError('adaptive repositories must share one native CAD database')
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
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

    def _validate_candidate_authority(self, plan: CadAdaptivePlan, spec) -> None:
        wanted = {proposal.candidate_id for proposal in plan.proposals}
        found: set[str] = set()
        pages = iter_cad_candidate_pages(
            self.search_repository.scene_repository,
            spec,
        )
        while wanted - found:
            page = next(pages, None)
            if page is None:
                break
            if page.candidate_set_sha256 != plan.candidate_set_sha256:
                raise ValueError('adaptive candidate-set hash no longer matches SearchSpec')
            found.update(
                candidate.candidate_id
                for candidate in page.candidates
                if candidate.candidate_id in wanted
            )
        missing = wanted - found
        if missing:
            raise ValueError(
                f'adaptive plan references candidates outside SearchSpec: {sorted(missing)}'
            )

    def save(self, plan: CadAdaptivePlan) -> None:
        if not isinstance(plan, CadAdaptivePlan):
            raise TypeError('plan must be CadAdaptivePlan')
        plan = CadAdaptivePlan.model_validate(plan.model_dump(mode='python'))

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
                raise ValueError('adaptive synthetic development gate is no longer satisfied')
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

        self._validate_candidate_authority(plan, spec)
        with closing(self._connect()) as connection, connection:
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

    def get(self, plan_id: str) -> CadAdaptivePlan | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_adaptive_plans WHERE plan_id=?',
                (plan_id,),
            ).fetchone()
        return None if row is None else CadAdaptivePlan.model_validate_json(
            row['payload_json']
        )

    def find_by_sha(
        self,
        search_spec_id: str,
        adaptive_sha256: str,
    ) -> CadAdaptivePlan | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_adaptive_plans '
                'WHERE search_spec_id=? AND adaptive_sha256=? '
                'ORDER BY seq DESC LIMIT 1',
                (search_spec_id, adaptive_sha256),
            ).fetchone()
        return None if row is None else CadAdaptivePlan.model_validate_json(
            row['payload_json']
        )

    def list_for_search_spec(self, search_spec_id: str) -> tuple[CadAdaptivePlan, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_adaptive_plans '
                'WHERE search_spec_id=? ORDER BY seq ASC',
                (search_spec_id,),
            ).fetchall()
        return tuple(
            CadAdaptivePlan.model_validate_json(row['payload_json'])
            for row in rows
        )
