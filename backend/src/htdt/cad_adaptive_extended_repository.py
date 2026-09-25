from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .cad_adaptive_extended import (
    CadAdaptiveExtendedObservation,
    CadAdaptiveExtendedPlan,
    CadAdaptiveObservationSourceRef,
    build_adaptive_extended_plan,
)
from .cad_adaptive_planner import (
    development_validation_ready,
    production_validation_ready,
)
from .cad_extended_search import generate_extended_candidates
from .cad_extended_search_repository import CadExtendedSearchRepository
from .cad_model_validation_repository import CadModelValidationRepository
from .cad_objective_repository import CadObjectiveRepository
from .cad_schema import require_native_tables


class AdaptiveObservationConflictError(ValueError):
    """An observation save violated the per-key single-head supersession contract."""


class CadAdaptiveExtendedRepository:
    """Immutable O80A evidence/plan storage bound to exact O80/O60 authority."""

    def __init__(
        self,
        extended_repository: CadExtendedSearchRepository,
        validation_repository: CadModelValidationRepository,
        objective_repository: CadObjectiveRepository | None = None,
    ) -> None:
        self.extended_repository = extended_repository
        self.validation_repository = validation_repository
        self.search_repository = extended_repository.search_repository
        self.path = Path(self.search_repository.path)
        if Path(validation_repository.path) != self.path:
            raise ValueError(
                'adaptive extended repositories must share one native CAD database'
            )
        if objective_repository is None:
            objective_repository = CadObjectiveRepository(
                self.search_repository.scene_repository,
                self.search_repository,
            )
        if Path(objective_repository.path) != self.path:
            raise ValueError(
                'adaptive extended and objective repositories must share one '
                'native CAD database'
            )
        self.objective_repository = objective_repository
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        # Kept idempotent so opening an already-migrated database is harmless.
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_adaptive_extended_observations',
                'cad_adaptive_extended_plans',
            )


    def _authority(self, extended_search_id: str):
        spec = self.extended_repository.get_spec(extended_search_id)
        if spec is None:
            raise ValueError('adaptive extended SearchSpec does not exist')
        base = self.search_repository.get(spec.base_search_spec_id)
        if base is None:
            raise ValueError('adaptive extended base SearchSpec does not exist')
        capability = self.extended_repository.get_capability(spec.capability_id)
        if capability is None:
            raise ValueError('adaptive extended capability does not exist')
        if capability.capability_sha256 != spec.capability_sha256:
            raise ValueError('adaptive extended capability hash mismatch')
        return spec, base, capability

    def _all_candidates(self, spec, base):
        candidates = []
        offset = 0
        seen_hash: str | None = None
        while True:
            page = generate_extended_candidates(
                self.search_repository.scene_repository,
                base,
                spec,
                offset=offset,
                limit=500,
            )
            if seen_hash is None:
                seen_hash = page.candidate_set_sha256
            elif page.candidate_set_sha256 != seen_hash:
                raise ValueError(
                    'adaptive extended candidate-set identity changed between pages'
                )
            candidates.extend(page.candidates)
            offset += len(page.candidates)
            if not page.candidates or offset >= page.feasible_candidate_count:
                break
        if seen_hash is None or not candidates:
            raise ValueError('adaptive extended SearchSpec has no feasible candidates')
        return tuple(candidates), seen_hash

    def save_observation(
        self,
        observation: CadAdaptiveExtendedObservation,
    ) -> None:
        """Append one observation as the single head of its evidence chain.

        The persisted history of one
        ``(extended_search_id, candidate_id, objective_id)`` key is an
        append-only chain enforced under one ``BEGIN IMMEDIATE``
        transaction: the first observation must claim no predecessor and
        every later observation must claim the exact current head via
        ``supersedes_observation_sha256``. The head is reloaded on the
        write connection inside the transaction, so two writers racing
        from the same head cannot both advance it — the loser sees the
        moved head and fails with ``AdaptiveObservationConflictError``.
        The persisted predecessor column and its uniqueness indexes keep
        the same single-head invariant at the storage layer, so no write
        path can leave two children claiming one head.

        The immutable SearchSpec/candidate/capability authorities are
        revalidated on every save before the lock is taken — they open
        their own connections, which must not run while BEGIN IMMEDIATE
        is held — so the builder remains a convenience and not the only
        integrity boundary.
        """
        observation = CadAdaptiveExtendedObservation.model_validate(
            observation.model_dump(mode='python')
        )
        spec, base, capability = self._authority(
            observation.extended_search_id
        )
        if spec.extended_search_sha256 != observation.extended_search_sha256:
            raise ValueError('adaptive extended observation SearchSpec hash mismatch')
        expected_scope = capability.evidence_scope
        if observation.evidence_scope != expected_scope:
            raise ValueError(
                'adaptive extended observation scope does not match capability'
            )

        candidates, candidate_set_sha256 = self._all_candidates(spec, base)
        if candidate_set_sha256 != observation.candidate_set_sha256:
            raise ValueError(
                'adaptive extended observation candidate-set hash mismatch'
            )
        if observation.candidate_id not in {
            candidate.candidate_id for candidate in candidates
        }:
            raise ValueError(
                'adaptive extended observation candidate is outside Extended SearchSpec'
            )
        candidate = next(
            item
            for item in candidates
            if item.candidate_id == observation.candidate_id
        )
        # Source refs are re-resolved before the write lock is taken: the
        # resolvers open their own connections, which must not run while
        # BEGIN IMMEDIATE is held.
        self._validate_observation_sources(observation, candidate.base_candidate_id)

        with closing(self._connect()) as connection, connection:
            # BEGIN IMMEDIATE holds the write lock across the duplicate
            # recheck, the per-key head reload and the insert: concurrent
            # writers cannot both observe the same head.
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute(
                'SELECT 1 FROM cad_adaptive_extended_observations '
                'WHERE observation_id=?',
                (observation.observation_id,),
            ).fetchone() is not None:
                raise ValueError(
                    'adaptive extended observation already exists: '
                    f'{observation.observation_id}'
                )
            head_row = connection.execute(
                """
                SELECT *
                FROM cad_adaptive_extended_observations
                WHERE extended_search_id=? AND candidate_id=? AND objective_id=?
                ORDER BY seq DESC
                LIMIT 1
                """,
                (
                    observation.extended_search_id,
                    observation.candidate_id,
                    observation.objective_id,
                ),
            ).fetchone()
            if head_row is None:
                if observation.supersedes_observation_sha256 is not None:
                    raise AdaptiveObservationConflictError(
                        'first adaptive extended observation must not supersede '
                        'another record'
                    )
            else:
                head = self._decode_observation_row(head_row)
                if (
                    observation.supersedes_observation_sha256
                    != head.observation_sha256
                ):
                    raise AdaptiveObservationConflictError(
                        'adaptive extended observation must supersede the '
                        'current record SHA'
                    )
            connection.execute(
                """
                INSERT INTO cad_adaptive_extended_observations(
                    observation_id, extended_search_id, candidate_id,
                    objective_id, observation_sha256,
                    supersedes_observation_sha256, payload_json,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.extended_search_id,
                    observation.candidate_id,
                    observation.objective_id,
                    observation.observation_sha256,
                    observation.supersedes_observation_sha256,
                    observation.model_dump_json(),
                    observation.created_at_utc,
                ),
            )

    def _resolve_observation_source(
        self,
        ref: CadAdaptiveObservationSourceRef,
        observation: CadAdaptiveExtendedObservation,
        base_candidate_id: str,
        *,
        role: str,
        required_evidence_class: str,
        claimed_value: float,
    ) -> None:
        """Require one typed source ref to resolve to exact authority (#382).

        ``objective_evaluation`` refs load the persisted O30 evaluation and
        require its exact semantic hash, the same base candidate the
        extended candidate derives from, an input ref of the required
        evidence class, and the referenced objective metric to reproduce
        the observation's declared unit and value. A missing, tampered, or
        unrelated source fails closed.

        ``synthetic_fixture`` refs are declared-only development claims; they
        cannot back ``owned_room`` evidence.
        """
        if ref.kind == 'synthetic_fixture':
            if observation.evidence_scope != 'synthetic_fixture':
                raise ValueError(
                    f'adaptive extended {role} source is a declared synthetic '
                    'fixture claim and cannot back owned-room evidence'
                )
            return

        evaluation = self.objective_repository.get_evaluation(ref.source_id)
        if evaluation is None:
            raise ValueError(
                f'adaptive extended {role} source does not resolve to a '
                f'persisted objective evaluation: {ref.source_id}'
            )
        if evaluation.evaluation_sha256 != ref.source_sha256:
            raise ValueError(
                f'adaptive extended {role} source hash mismatch: '
                f'{ref.source_id}'
            )
        # O30 evaluations are keyed by the base SearchSpec candidate the
        # extended candidate is derived from; a source recorded for another
        # candidate cannot back this observation.
        if evaluation.candidate_id != base_candidate_id:
            raise ValueError(
                f'adaptive extended {role} source belongs to a different '
                'candidate'
            )
        if not any(
            input_ref.evidence_class == required_evidence_class
            for input_ref in evaluation.input_refs
        ):
            raise ValueError(
                f'adaptive extended {role} source evaluation carries no '
                f'{required_evidence_class} evidence'
            )
        try:
            metric = evaluation.vector.metric(observation.objective_id)
        except KeyError as exc:
            raise ValueError(
                f'adaptive extended {role} source does not report objective '
                f'{observation.objective_id}'
            ) from exc
        if metric.unit != observation.unit:
            raise ValueError(
                f'adaptive extended {role} source unit mismatch'
            )
        if metric.value != claimed_value:
            raise ValueError(
                f'adaptive extended {role} source value does not equal the '
                'stored observation metric'
            )

    def _validate_observation_sources(
        self,
        observation: CadAdaptiveExtendedObservation,
        base_candidate_id: str,
    ) -> None:
        """Replay the exact evidence authority behind every stored value."""

        self._resolve_observation_source(
            observation.prediction_source,
            observation,
            base_candidate_id,
            role='prediction',
            required_evidence_class='predicted',
            claimed_value=float(observation.predicted_value),
        )
        if observation.measured_value is not None:
            # The model validator guarantees a measurement source exists
            # whenever a measured value does.
            assert observation.measurement_source is not None
            self._resolve_observation_source(
                observation.measurement_source,
                observation,
                base_candidate_id,
                role='measurement',
                required_evidence_class='measured',
                claimed_value=float(observation.measured_value),
            )

    def _extended_candidate_map(self, extended_search_id: str):
        spec, base, _capability = self._authority(extended_search_id)
        candidates, _candidate_set_sha256 = self._all_candidates(spec, base)
        return {candidate.candidate_id: candidate for candidate in candidates}

    def _read_observation_row(
        self,
        row: sqlite3.Row,
        candidate_map: dict[str, object] | None = None,
    ) -> CadAdaptiveExtendedObservation:
        observation = self._decode_observation_row(row)
        if candidate_map is None:
            candidate_map = self._extended_candidate_map(
                observation.extended_search_id
            )
        candidate = candidate_map.get(observation.candidate_id)
        if candidate is None:
            raise ValueError(
                'persisted adaptive extended observation references a '
                'candidate outside the Extended SearchSpec'
            )
        self._validate_observation_sources(
            observation,
            candidate.base_candidate_id,
        )
        return observation

    @staticmethod
    def _decode_observation_row(
        row: sqlite3.Row,
    ) -> CadAdaptiveExtendedObservation:
        """Deserialize one persisted observation row and verify its columns.

        Rows written before #382 carried free-form source strings that never
        resolved to authority; they cannot prove source binding and fail
        closed on read instead of silently passing.
        """

        if '"prediction_source"' not in row['payload_json']:
            raise ValueError(
                'persisted adaptive extended observation uses pre-#382 '
                'untyped source fields and cannot prove source authority'
            )
        observation = CadAdaptiveExtendedObservation.model_validate_json(
            row['payload_json']
        )
        if (
            row['observation_id'] != observation.observation_id
            or row['extended_search_id'] != observation.extended_search_id
            or row['candidate_id'] != observation.candidate_id
            or row['objective_id'] != observation.objective_id
            or row['observation_sha256'] != observation.observation_sha256
            or row['supersedes_observation_sha256']
            != observation.supersedes_observation_sha256
            or row['created_at_utc'] != observation.created_at_utc
        ):
            raise ValueError(
                'persisted adaptive extended observation row disagrees '
                'with its payload'
            )
        return observation

    def get_observation(
        self,
        observation_id: str,
    ) -> CadAdaptiveExtendedObservation | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * '
                'FROM cad_adaptive_extended_observations '
                'WHERE observation_id=?',
                (observation_id,),
            ).fetchone()
        return None if row is None else self._read_observation_row(row)

    def current_observations(
        self,
        extended_search_id: str,
    ) -> tuple[CadAdaptiveExtendedObservation, ...]:
        current: dict[
            tuple[str, str],
            CadAdaptiveExtendedObservation,
        ] = {}
        for observation in self.list_observations(extended_search_id):
            key = (observation.candidate_id, observation.objective_id)
            previous = current.get(key)
            if previous is None:
                if observation.supersedes_observation_sha256 is not None:
                    raise ValueError(
                        'adaptive extended observation chain has invalid root'
                    )
            elif (
                observation.supersedes_observation_sha256
                != previous.observation_sha256
            ):
                raise ValueError(
                    'adaptive extended observation chain is broken'
                )
            current[key] = observation
        return tuple(
            current[key]
            for key in sorted(current)
        )

    def list_observations(
        self,
        extended_search_id: str,
    ) -> tuple[CadAdaptiveExtendedObservation, ...]:
        """Return the search's observation history with column-level integrity.

        Rows are replayed in insertion order; every stored column —
        including the persisted predecessor hash — must agree with its
        payload, so a tampered or drifted row fails closed rather than
        silently steering the supersession-head CAS.
        """
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * '
                'FROM cad_adaptive_extended_observations '
                'WHERE extended_search_id=? ORDER BY seq ASC',
                (extended_search_id,),
            ).fetchall()
        if not rows:
            return ()
        candidate_map = self._extended_candidate_map(extended_search_id)
        return tuple(
            self._read_observation_row(row, candidate_map) for row in rows
        )

    def _require_plan_authority(self, plan: CadAdaptiveExtendedPlan) -> None:
        """Replay the exact O80/O60/observation authority one plan binds to.

        Revalidates the base/extended SearchSpec and capability binding, the
        referenced O60 ValidationRecord and its current scope gate, and the
        regenerated extended candidate set; resolves the current single-head
        observation chains; then reruns build_adaptive_extended_plan over the
        persisted algorithm inputs and requires the submitted plan to be that
        exact canonical output. Superseded, stale, or caller-fabricated
        evidence fails closed. Shared by save-time validation and every
        authoritative plan read.
        """
        spec, base, capability = self._authority(plan.extended_search_id)
        if (
            plan.document_id != base.document_id
            or plan.base_search_spec_id != base.search_spec_id
            or plan.base_search_spec_sha256 != base.search_spec_sha256
            or plan.base_candidate_set_sha256
            != spec.base_candidate_set_sha256
        ):
            raise ValueError('adaptive extended base SearchSpec authority mismatch')
        if (
            plan.extended_search_sha256 != spec.extended_search_sha256
            or plan.capability_id != capability.capability_id
            or plan.capability_sha256 != capability.capability_sha256
            or plan.extended_model_id != capability.model_id
            or plan.extended_model_version != capability.model_version
        ):
            raise ValueError('adaptive extended O80 authority mismatch')

        validation = self.validation_repository.get(plan.validation_id)
        if validation is None:
            raise ValueError('adaptive extended O60 ValidationRecord does not exist')
        if validation.validation_sha256 != plan.validation_sha256:
            raise ValueError('adaptive extended O60 ValidationRecord hash mismatch')
        if (
            validation.search_spec_id != plan.base_search_spec_id
            or validation.search_spec_sha256 != plan.base_search_spec_sha256
            or validation.candidate_set_sha256
            != plan.base_candidate_set_sha256
            or validation.model_id != plan.base_model_id
            or validation.model_version != plan.base_model_version
        ):
            raise ValueError('adaptive extended O60/base authority mismatch')

        if plan.execution_scope == 'development_synthetic':
            if not development_validation_ready(validation):
                raise ValueError(
                    'adaptive extended synthetic development gate is not satisfied'
                )
            if capability.evidence_scope != 'synthetic_fixture':
                raise ValueError(
                    'adaptive extended synthetic plan requires synthetic capability'
                )
        else:
            if not production_validation_ready(validation):
                raise ValueError(
                    'adaptive extended production O60 gate is not satisfied'
                )
            current = self.validation_repository.latest_eligible_for_search_spec(
                plan.base_search_spec_id
            )
            if current is None or current.validation_id != validation.validation_id:
                raise ValueError(
                    'adaptive extended production plan must use current O70-entry validation'
                )
            if capability.evidence_scope != 'owned_room':
                raise ValueError(
                    'adaptive extended production plan requires owned-room capability'
                )
            if capability.validation_id != validation.validation_id:
                raise ValueError(
                    'adaptive extended production capability must use exact validation'
                )

        candidates, candidate_set_sha256 = self._all_candidates(spec, base)
        if candidate_set_sha256 != plan.extended_candidate_set_sha256:
            raise ValueError(
                'adaptive extended candidate-set hash no longer matches O80 authority'
            )
        candidate_ids = {candidate.candidate_id for candidate in candidates}
        referenced = (
            {proposal.candidate_id for proposal in plan.proposals}
            | set(plan.training_candidate_ids)
            | set(plan.excluded_measured_candidate_ids)
        )
        missing = referenced - candidate_ids
        if missing:
            raise ValueError(
                'adaptive extended plan references candidates outside O80 set: '
                + ', '.join(sorted(missing))
            )

        observations = self.current_observations(plan.extended_search_id)
        current_hashes = {
            observation.observation_sha256 for observation in observations
        }
        stale_hashes = set(plan.observation_sha256s) - current_hashes
        if stale_hashes:
            raise ValueError(
                'adaptive extended plan observation authority is not current'
            )

        expected = build_adaptive_extended_plan(
            base_spec=base,
            base_candidate_set_sha256=spec.base_candidate_set_sha256,
            extended_spec=spec,
            extended_candidate_set_sha256=candidate_set_sha256,
            capability_id=capability.capability_id,
            capability_sha256=capability.capability_sha256,
            extended_model_id=capability.model_id,
            extended_model_version=capability.model_version,
            validation=validation,
            candidates=candidates,
            observations=observations,
            execution_scope=plan.execution_scope,
            length_scale_normalized=plan.length_scale_normalized,
            proposal_limit=plan.proposal_limit,
        )
        if expected.adaptive_extended_sha256 != plan.adaptive_extended_sha256:
            raise ValueError(
                'adaptive extended plan does not match canonical planner replay'
            )

    def save_plan(self, plan: CadAdaptiveExtendedPlan) -> None:
        plan = CadAdaptiveExtendedPlan.model_validate(
            plan.model_dump(mode='python')
        )
        self._require_plan_authority(plan)

        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_adaptive_extended_plans(
                    plan_id, document_id, extended_search_id, validation_id,
                    execution_scope, selected_candidate_id,
                    adaptive_extended_sha256, payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.document_id,
                    plan.extended_search_id,
                    plan.validation_id,
                    plan.execution_scope,
                    plan.selected_candidate_id,
                    plan.adaptive_extended_sha256,
                    plan.model_dump_json(),
                    plan.created_at_utc,
                ),
            )

    def _validated_plan(self, row: sqlite3.Row) -> CadAdaptiveExtendedPlan:
        """Deserialize one persisted plan row and replay its exact authority."""

        plan = CadAdaptiveExtendedPlan.model_validate_json(row['payload_json'])
        if (
            row['plan_id'] != plan.plan_id
            or row['document_id'] != plan.document_id
            or row['extended_search_id'] != plan.extended_search_id
            or row['validation_id'] != plan.validation_id
            or row['execution_scope'] != plan.execution_scope
            or row['selected_candidate_id'] != plan.selected_candidate_id
            or row['adaptive_extended_sha256'] != plan.adaptive_extended_sha256
            or row['created_at_utc'] != plan.created_at_utc
        ):
            raise ValueError(
                'persisted adaptive extended plan row disagrees with its payload'
            )
        self._require_plan_authority(plan)
        return plan

    def get_plan(self, plan_id: str) -> CadAdaptiveExtendedPlan | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_adaptive_extended_plans WHERE plan_id=?',
                (plan_id,),
            ).fetchone()
        return None if row is None else self._validated_plan(row)

    def find_plan_by_sha(
        self,
        extended_search_id: str,
        adaptive_extended_sha256: str,
    ) -> CadAdaptiveExtendedPlan | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_adaptive_extended_plans '
                'WHERE extended_search_id=? AND adaptive_extended_sha256=? '
                'ORDER BY seq DESC LIMIT 1',
                (extended_search_id, adaptive_extended_sha256),
            ).fetchone()
        return None if row is None else self._validated_plan(row)

    def list_plans(
        self,
        extended_search_id: str,
    ) -> tuple[CadAdaptiveExtendedPlan, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM cad_adaptive_extended_plans '
                'WHERE extended_search_id=? ORDER BY seq ASC',
                (extended_search_id,),
            ).fetchall()
        return tuple(self._validated_plan(row) for row in rows)

def run_adaptive_extended_schema_convergence(
    connection: sqlite3.Connection,
) -> None:
    """Legacy-shape tail of the schema-authority migration (#302).

    Plain ``CREATE TABLE`` lives in ``cad_schema_ddl`` and runs inside
    the versioned migration; this sequence converges databases whose
    persisted shapes predate the canonical contract (predecessor-column
    backfill, fork detection, then the uniqueness guards) and installs
    the adaptive-extended tables so every supported open path converges
    the same way.
    """
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS cad_adaptive_extended_observations (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            observation_id TEXT NOT NULL UNIQUE,
            extended_search_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            objective_id TEXT NOT NULL,
            observation_sha256 TEXT NOT NULL UNIQUE,
            supersedes_observation_sha256 TEXT,
            payload_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_adaptive_extended_observation_search_seq
            ON cad_adaptive_extended_observations(
                extended_search_id, seq ASC
            );
        CREATE INDEX IF NOT EXISTS idx_adaptive_extended_observation_key_seq
            ON cad_adaptive_extended_observations(
                extended_search_id, candidate_id, objective_id, seq ASC
            );

        CREATE TABLE IF NOT EXISTS cad_adaptive_extended_plans (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id TEXT NOT NULL UNIQUE,
            document_id TEXT NOT NULL,
            extended_search_id TEXT NOT NULL,
            validation_id TEXT NOT NULL,
            execution_scope TEXT NOT NULL,
            selected_candidate_id TEXT NOT NULL,
            adaptive_extended_sha256 TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_adaptive_extended_plan_search_seq
            ON cad_adaptive_extended_plans(
                extended_search_id, seq ASC
            );
        """
    )
    # Predecessor-column migration: rows written before the persisted
    # predecessor hash existed are backfilled from their payload so the
    # supersession edge is a dedicated indexed column. A persisted fork
    # — two children claiming one head, or two roots for one key — is
    # reported explicitly before the uniqueness guards are installed,
    # instead of failing opaquely or letting a read pick a winner by
    # insertion order.
    columns = {
        row['name']
        for row in connection.execute(
            'PRAGMA table_info(cad_adaptive_extended_observations)'
        )
    }
    if 'supersedes_observation_sha256' not in columns:
        connection.execute(
            'ALTER TABLE cad_adaptive_extended_observations '
            'ADD COLUMN supersedes_observation_sha256 TEXT'
        )
        for row in connection.execute(
            'SELECT observation_id, payload_json '
            'FROM cad_adaptive_extended_observations'
        ).fetchall():
            connection.execute(
                'UPDATE cad_adaptive_extended_observations '
                'SET supersedes_observation_sha256=? '
                'WHERE observation_id=?',
                (
                    json.loads(row['payload_json']).get(
                        'supersedes_observation_sha256'
                    ),
                    row['observation_id'],
                ),
            )
    forked_heads = connection.execute(
        """
        SELECT supersedes_observation_sha256
        FROM cad_adaptive_extended_observations
        WHERE supersedes_observation_sha256 IS NOT NULL
        GROUP BY supersedes_observation_sha256
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    if forked_heads:
        raise ValueError(
            'adaptive extended observation history has a supersession '
            'fork: '
            + ', '.join(
                sorted(
                    row['supersedes_observation_sha256']
                    for row in forked_heads
                )
            )
            + ' claimed by more than one child'
        )
    forked_roots = connection.execute(
        """
        SELECT extended_search_id, candidate_id, objective_id
        FROM cad_adaptive_extended_observations
        WHERE supersedes_observation_sha256 IS NULL
        GROUP BY extended_search_id, candidate_id, objective_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    if forked_roots:
        raise ValueError(
            'adaptive extended observation history has a supersession '
            'fork: '
            + ', '.join(
                sorted(
                    f"({row['extended_search_id']}, "
                    f"{row['candidate_id']}, {row['objective_id']})"
                    for row in forked_roots
                )
            )
            + ' has more than one root observation'
        )
    # Single-head storage invariants: one claimed head may be
    # superseded at most once, and each observation key may persist at
    # most one predecessor-free root.
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
            idx_adaptive_extended_observation_supersedes
        ON cad_adaptive_extended_observations(
            supersedes_observation_sha256
        ) WHERE supersedes_observation_sha256 IS NOT NULL
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
            idx_adaptive_extended_observation_root
        ON cad_adaptive_extended_observations(
            extended_search_id, candidate_id, objective_id
        ) WHERE supersedes_observation_sha256 IS NULL
        """
    )


