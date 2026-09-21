from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from .cad_objective_models import CadObjectiveEvaluation, CadParetoSet
from .cad_repository import SceneRepository
from .cad_search_repository import CadSearchRepository
from .pareto import pareto_front


class CadObjectiveRepository:
    """Immutable objective/Pareto storage bound to native SceneRevision and SearchSpec authority."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        search_repository: CadSearchRepository,
    ) -> None:
        self.scene_repository = scene_repository
        self.search_repository = search_repository
        self.path = Path(scene_repository.path)
        if Path(search_repository.path) != self.path:
            raise ValueError('scene and search repositories must share one native CAD database')
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                '''
                CREATE TABLE IF NOT EXISTS cad_objective_evaluations (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    evaluation_id TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    scene_revision_id TEXT NOT NULL,
                    scene_content_hash TEXT NOT NULL,
                    search_spec_id TEXT NOT NULL,
                    search_spec_sha256 TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    evaluation_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id),
                    FOREIGN KEY(search_spec_id) REFERENCES cad_search_specs(search_spec_id)
                );
                CREATE INDEX IF NOT EXISTS idx_objective_search_seq
                    ON cad_objective_evaluations(search_spec_id, seq ASC);

                CREATE TABLE IF NOT EXISTS cad_pareto_sets (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    pareto_set_id TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    scene_revision_id TEXT NOT NULL,
                    scene_content_hash TEXT NOT NULL,
                    search_spec_id TEXT NOT NULL,
                    search_spec_sha256 TEXT NOT NULL,
                    pareto_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id),
                    FOREIGN KEY(search_spec_id) REFERENCES cad_search_specs(search_spec_id)
                );
                CREATE INDEX IF NOT EXISTS idx_pareto_search_seq
                    ON cad_pareto_sets(search_spec_id, seq ASC);
                '''
            )

    def _validate_binding(
        self,
        document_id: str,
        scene_revision_id: str,
        scene_content_hash: str,
        search_spec_id: str,
        search_spec_sha256: str,
    ) -> None:
        revision = self.scene_repository.get(scene_revision_id)
        if revision is None:
            raise ValueError('objective source revision does not exist')
        if revision.document_id != document_id:
            raise ValueError('objective source revision belongs to another document')
        if revision.content_hash != scene_content_hash:
            raise ValueError('objective source content hash does not match revision')

        spec = self.search_repository.get(search_spec_id)
        if spec is None:
            raise ValueError('objective SearchSpec does not exist')
        if spec.document_id != document_id:
            raise ValueError('objective SearchSpec belongs to another document')
        if spec.scene_revision_id != scene_revision_id or spec.scene_content_hash != scene_content_hash:
            raise ValueError('objective SearchSpec source binding mismatch')
        if spec.search_spec_sha256 != search_spec_sha256:
            raise ValueError('objective SearchSpec hash mismatch')

    def save_evaluation(self, evaluation: CadObjectiveEvaluation) -> None:
        self._validate_binding(
            evaluation.document_id,
            evaluation.scene_revision_id,
            evaluation.scene_content_hash,
            evaluation.search_spec_id,
            evaluation.search_spec_sha256,
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                '''INSERT INTO cad_objective_evaluations(
                    evaluation_id, document_id, scene_revision_id, scene_content_hash,
                    search_spec_id, search_spec_sha256, candidate_id, evaluation_sha256,
                    payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    evaluation.evaluation_id,
                    evaluation.document_id,
                    evaluation.scene_revision_id,
                    evaluation.scene_content_hash,
                    evaluation.search_spec_id,
                    evaluation.search_spec_sha256,
                    evaluation.candidate_id,
                    evaluation.evaluation_sha256,
                    evaluation.model_dump_json(),
                    evaluation.created_at_utc,
                ),
            )

    def get_evaluation(self, evaluation_id: str) -> CadObjectiveEvaluation | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_objective_evaluations WHERE evaluation_id=?',
                (evaluation_id,),
            ).fetchone()
        return None if row is None else CadObjectiveEvaluation.model_validate_json(row['payload_json'])

    def list_evaluations(self, search_spec_id: str) -> tuple[CadObjectiveEvaluation, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_objective_evaluations WHERE search_spec_id=? ORDER BY seq ASC',
                (search_spec_id,),
            ).fetchall()
        return tuple(CadObjectiveEvaluation.model_validate_json(row['payload_json']) for row in rows)

    def _require_pareto_authority(self, pareto_set: CadParetoSet) -> None:
        """Replay the exact authority one Pareto set is bound to.

        Revalidates the SceneRevision/SearchSpec binding, reloads every
        referenced O30 evaluation through the evaluation read path,
        verifies exact evaluation SHA/candidate/binding, and requires the
        canonical recomputed front to equal the submitted result. Missing
        or mismatched authority fails closed; used by both save-time
        validation and authoritative reads.
        """

        self._validate_binding(
            pareto_set.document_id,
            pareto_set.scene_revision_id,
            pareto_set.scene_content_hash,
            pareto_set.search_spec_id,
            pareto_set.search_spec_sha256,
        )

        evaluations: list[CadObjectiveEvaluation] = []
        for ref in pareto_set.evaluations:
            evaluation = self.get_evaluation(ref.evaluation_id)
            if evaluation is None:
                raise ValueError(f'Pareto objective evaluation does not exist: {ref.evaluation_id}')
            if evaluation.evaluation_id != ref.evaluation_id:
                raise ValueError('Pareto objective evaluation id mismatch')
            if evaluation.evaluation_sha256 != ref.evaluation_sha256:
                raise ValueError('Pareto objective evaluation hash mismatch')
            if evaluation.candidate_id != ref.candidate_id:
                raise ValueError('Pareto objective candidate mismatch')
            if (
                evaluation.document_id != pareto_set.document_id
                or evaluation.scene_revision_id != pareto_set.scene_revision_id
                or evaluation.scene_content_hash != pareto_set.scene_content_hash
                or evaluation.search_spec_id != pareto_set.search_spec_id
                or evaluation.search_spec_sha256 != pareto_set.search_spec_sha256
            ):
                raise ValueError('Pareto objective evaluation binding mismatch')
            evaluations.append(evaluation)

        expected = pareto_front(
            tuple(item.vector for item in evaluations),
            pareto_set.objective_ids,
        )
        if expected != pareto_set.result:
            raise ValueError('Pareto result does not match referenced objective evaluations')

    def _validated_pareto_set(self, row: sqlite3.Row) -> CadParetoSet:
        """Deserialize one persisted Pareto row and replay its exact authority."""

        pareto_set = CadParetoSet.model_validate_json(row['payload_json'])
        if (
            row['pareto_set_id'] != pareto_set.pareto_set_id
            or row['document_id'] != pareto_set.document_id
            or row['scene_revision_id'] != pareto_set.scene_revision_id
            or row['scene_content_hash'] != pareto_set.scene_content_hash
            or row['search_spec_id'] != pareto_set.search_spec_id
            or row['search_spec_sha256'] != pareto_set.search_spec_sha256
            or row['pareto_sha256'] != pareto_set.pareto_sha256
            or row['created_at_utc'] != pareto_set.created_at_utc
        ):
            raise ValueError('persisted CadParetoSet row disagrees with its payload')
        self._require_pareto_authority(pareto_set)
        return pareto_set

    def save_pareto_set(self, pareto_set: CadParetoSet) -> None:
        self._require_pareto_authority(pareto_set)

        with closing(self._connect()) as connection, connection:
            connection.execute(
                '''INSERT INTO cad_pareto_sets(
                    pareto_set_id, document_id, scene_revision_id, scene_content_hash,
                    search_spec_id, search_spec_sha256, pareto_sha256, payload_json,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    pareto_set.pareto_set_id,
                    pareto_set.document_id,
                    pareto_set.scene_revision_id,
                    pareto_set.scene_content_hash,
                    pareto_set.search_spec_id,
                    pareto_set.search_spec_sha256,
                    pareto_set.pareto_sha256,
                    pareto_set.model_dump_json(),
                    pareto_set.created_at_utc,
                ),
            )

    def find_pareto_set_by_sha(self, search_spec_id: str, pareto_sha256: str) -> CadParetoSet | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_pareto_sets WHERE search_spec_id=? AND pareto_sha256=? '
                'ORDER BY seq DESC LIMIT 1',
                (search_spec_id, pareto_sha256),
            ).fetchone()
        return None if row is None else self._validated_pareto_set(row)

    def get_pareto_set(self, pareto_set_id: str) -> CadParetoSet | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_pareto_sets WHERE pareto_set_id=?',
                (pareto_set_id,),
            ).fetchone()
        return None if row is None else self._validated_pareto_set(row)

    def latest_evaluations_by_candidate(
        self,
        search_spec_id: str,
    ) -> tuple[CadObjectiveEvaluation, ...]:
        """Return the latest immutable evaluation for each candidate, preserving candidate first-seen order."""
        evaluations = self.list_evaluations(search_spec_id)
        order: list[str] = []
        latest: dict[str, CadObjectiveEvaluation] = {}
        for evaluation in evaluations:
            if evaluation.candidate_id not in latest:
                order.append(evaluation.candidate_id)
            latest[evaluation.candidate_id] = evaluation
        return tuple(latest[candidate_id] for candidate_id in order)

    def list_pareto_sets(self, search_spec_id: str) -> tuple[CadParetoSet, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM cad_pareto_sets WHERE search_spec_id=? ORDER BY seq ASC',
                (search_spec_id,),
            ).fetchall()
        return tuple(self._validated_pareto_set(row) for row in rows)
