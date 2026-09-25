from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterator, Mapping, NamedTuple

from .cad_objective_authority import (
    DECLARED_ONLY_EVIDENCE_CLASSES,
    OBJECTIVE_INPUT_RESOLVERS,
    OBJECTIVE_VECTOR_EVALUATORS,
    ObjectiveAuthorityContext,
    ResolvedObjectiveInput,
    objective_spec_authority,
)
from .cad_objective_models import (
    CadObjectiveEvaluation,
    CadObjectiveInputRef,
    CadParetoSet,
    canonical_objective_json,
)
from .cad_repository import SceneRepository, SceneRevision
from .cad_search import iter_cad_candidate_pages
from .cad_search_models import CadCandidate, CadCandidateSetPage, CadSearchSpec
from .cad_search_repository import CadSearchRepository
from .optimization_objectives import ObjectiveVector
from .pareto import pareto_front
from .cad_schema import require_native_tables


class _CandidateSetScan:
    """Incremental replay memo for one SearchSpec's canonical candidate set.

    Membership replay pages through ``iter_cad_candidate_pages`` once per
    SearchSpec; ``list_evaluations`` shares one scan across rows so a column
    of evaluations does not regenerate the set per row.
    """

    __slots__ = ('candidate_set_sha256', 'members', 'pages', 'exhausted')

    def __init__(self, pages: Iterator[CadCandidateSetPage]) -> None:
        self.candidate_set_sha256: str | None = None
        self.members: dict[str, CadCandidate] = {}
        self.pages = pages
        self.exhausted = False


class _EvaluationAuthority(NamedTuple):
    candidate_set_sha256: str
    inputs: tuple[ResolvedObjectiveInput, ...]


class CadObjectiveRepository:
    """Immutable objective/Pareto storage bound to native SceneRevision and SearchSpec authority.

    O30 persistence is evidence-derived: ``save_evaluation`` and every
    authoritative read replay the exact SceneRevision/SearchSpec binding,
    regenerate the canonical candidate set to prove membership, resolve each
    input reference against persisted evidence, and require the stored vector
    to equal the vector the versioned ``evaluation_spec`` recomputes from that
    evidence. Rows written before the authority columns existed stay
    non-authoritative and fail closed on read rather than acquiring a
    fabricated current hash.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        search_repository: CadSearchRepository,
        *,
        measurement_repository: Any = None,
        roomsim_repository: Any = None,
        prediction_provider_repository: Any = None,
        hybrid_provider_repository: Any = None,
        input_resolvers: Mapping[
            str,
            Callable[[ObjectiveAuthorityContext, CadObjectiveInputRef], ResolvedObjectiveInput],
        ]
        | None = None,
        vector_evaluators: Mapping[
            str,
            Callable[[ObjectiveAuthorityContext], ObjectiveVector],
        ]
        | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.search_repository = search_repository
        self.path = Path(scene_repository.path)
        if Path(search_repository.path) != self.path:
            raise ValueError('scene and search repositories must share one native CAD database')
        for label, repository in (
            ('measurement', measurement_repository),
            ('roomsim', roomsim_repository),
            ('prediction provider', prediction_provider_repository),
            ('hybrid prediction provider', hybrid_provider_repository),
        ):
            if repository is not None and Path(repository.path) != self.path:
                raise ValueError(
                    f'objective and {label} repositories must share one native CAD database'
                )
        self._measurement_repository = measurement_repository
        self._roomsim_repository = roomsim_repository
        self.prediction_provider_repository = prediction_provider_repository
        self.hybrid_provider_repository = hybrid_provider_repository
        self._input_resolvers = {
            **OBJECTIVE_INPUT_RESOLVERS,
            **dict(input_resolvers or {}),
        }
        self._vector_evaluators = {
            **OBJECTIVE_VECTOR_EVALUATORS,
            **dict(vector_evaluators or {}),
        }
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _measurements(self) -> Any:
        if self._measurement_repository is None:
            from .cad_measurement_repository import CadMeasurementRepository

            self._measurement_repository = CadMeasurementRepository(
                self.scene_repository
            )
        return self._measurement_repository

    def _roomsim(self) -> Any:
        if self._roomsim_repository is None:
            from .cad_roomsim_repository import CadRoomSimRepository

            self._roomsim_repository = CadRoomSimRepository(
                self.scene_repository,
                self.search_repository,
            )
        return self._roomsim_repository

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_objective_evaluations', 'cad_pareto_sets')

    def _resolve_binding(
        self,
        document_id: str,
        scene_revision_id: str,
        scene_content_hash: str,
        search_spec_id: str,
        search_spec_sha256: str,
    ) -> tuple[SceneRevision, CadSearchSpec]:
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
        return revision, spec

    def _require_candidate_membership(
        self,
        search_spec: CadSearchSpec,
        candidate_id: str,
        scans: dict[str, _CandidateSetScan] | None = None,
    ) -> tuple[CadCandidate, str]:
        """Replay canonical candidate generation and require exact membership.

        A fabricated ``candidate_id`` cannot become O30 authority: the
        persisted SearchSpec must regenerate a candidate set containing this
        exact member, and every page must agree on the same
        ``candidate_set_sha256`` so the attested set identity is the canonical
        one. ``scans`` optionally shares one replay across a batch of
        evaluations bound to the same SearchSpec.
        """

        scan = None if scans is None else scans.get(search_spec.search_spec_sha256)
        if scan is None:
            scan = _CandidateSetScan(
                iter_cad_candidate_pages(self.scene_repository, search_spec)
            )
            if scans is not None:
                scans[search_spec.search_spec_sha256] = scan
        candidate = scan.members.get(candidate_id)
        if candidate is not None:
            assert scan.candidate_set_sha256 is not None
            return candidate, scan.candidate_set_sha256

        while not scan.exhausted:
            page = next(scan.pages, None)
            if page is None:
                scan.exhausted = True
                break
            scan.candidate_set_sha256 = page.candidate_set_sha256
            for item in page.candidates:
                scan.members[item.candidate_id] = item
            candidate = scan.members.get(candidate_id)
            if candidate is not None:
                return candidate, scan.candidate_set_sha256
        raise ValueError(
            'objective candidate is not a member of the SearchSpec candidate set'
        )

    def _resolve_input(
        self,
        context: ObjectiveAuthorityContext,
        ref: CadObjectiveInputRef,
    ) -> ResolvedObjectiveInput:
        resolver = self._input_resolvers.get(ref.source_kind)
        if resolver is None:
            if ref.evidence_class not in DECLARED_ONLY_EVIDENCE_CLASSES:
                raise ValueError(
                    f'objective {ref.evidence_class} input has no registered '
                    f'authority for source kind: {ref.source_kind}'
                )
            resolved = ResolvedObjectiveInput(
                ref=ref,
                source_sha256=ref.source_sha256,
            )
        else:
            resolved = resolver(context, ref)
            if not isinstance(resolved, ResolvedObjectiveInput):
                raise ValueError(
                    'objective input resolver must return ResolvedObjectiveInput'
                )
        if ref.source_sha256 is not None:
            if resolved.source_sha256 is None:
                raise ValueError(
                    f'objective input cannot attest the declared source hash: '
                    f'{ref.source_kind}:{ref.source_id}'
                )
            if resolved.source_sha256 != ref.source_sha256:
                raise ValueError(
                    f'objective input source hash mismatch: '
                    f'{ref.source_kind}:{ref.source_id}'
                )
        return resolved

    def _require_evaluation_authority(
        self,
        evaluation: CadObjectiveEvaluation,
        *,
        scans: dict[str, _CandidateSetScan] | None = None,
    ) -> _EvaluationAuthority:
        """Replay the exact authority one objective evaluation claims.

        Revalidates the SceneRevision/SearchSpec binding, regenerates the
        canonical candidate set to prove ``candidate_id`` membership and pin
        the candidate-set identity, resolves every input reference against
        persisted evidence, and recomputes the objective vector from the
        versioned ``evaluation_spec``. The stored vector must equal the
        replayed vector exactly — a coherently rehashed row whose numbers
        were altered still fails closed. Shared by save-time validation and
        every authoritative read.
        """

        revision, search_spec = self._resolve_binding(
            evaluation.document_id,
            evaluation.scene_revision_id,
            evaluation.scene_content_hash,
            evaluation.search_spec_id,
            evaluation.search_spec_sha256,
        )
        spec_payload = json.loads(evaluation.evaluation_spec_json)
        if not isinstance(spec_payload, dict):
            raise ValueError('objective evaluation spec must be a JSON object')
        authority_key = objective_spec_authority(spec_payload)
        evaluator = self._vector_evaluators.get(authority_key)
        if evaluator is None:
            raise ValueError(
                'objective evaluation spec authority is not registered for '
                f'replay: {authority_key}'
            )
        candidate, candidate_set_sha256 = self._require_candidate_membership(
            search_spec,
            evaluation.candidate_id,
            scans,
        )

        input_kinds = {ref.source_kind for ref in evaluation.input_refs}
        context = ObjectiveAuthorityContext(
            evaluation=evaluation,
            scene_repository=self.scene_repository,
            source_revision=revision,
            search_spec=search_spec,
            candidate=candidate,
            candidate_set_sha256=candidate_set_sha256,
            measurement_repository=(
                self._measurements() if 'cad_measurement' in input_kinds else None
            ),
            roomsim_repository=(
                self._roomsim() if 'cad_roomsim_attempt' in input_kinds else None
            ),
            prediction_provider_repository=(
                self.prediction_provider_repository
                if 'r170a_prediction_provider' in input_kinds
                else None
            ),
            hybrid_provider_repository=(
                self.hybrid_provider_repository
                if 'r170b_hybrid_prediction_provider' in input_kinds
                else None
            ),
            spec=spec_payload,
        )
        resolved = tuple(
            self._resolve_input(context, ref) for ref in evaluation.input_refs
        )
        context = context._replace(inputs=resolved)
        vector = evaluator(context)
        if vector != evaluation.vector:
            raise ValueError(
                'objective vector does not reproduce from persisted evidence'
            )
        return _EvaluationAuthority(
            candidate_set_sha256=candidate_set_sha256,
            inputs=resolved,
        )

    @staticmethod
    def _input_authorities_json(authority: _EvaluationAuthority) -> str:
        return canonical_objective_json([
            {
                'evidence_class': item.ref.evidence_class,
                'source_kind': item.ref.source_kind,
                'source_id': item.ref.source_id,
                'source_sha256': item.source_sha256,
            }
            for item in authority.inputs
        ])

    def save_evaluation(self, evaluation: CadObjectiveEvaluation) -> None:
        authority = self._require_evaluation_authority(evaluation)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                '''INSERT INTO cad_objective_evaluations(
                    evaluation_id, document_id, scene_revision_id, scene_content_hash,
                    search_spec_id, search_spec_sha256, candidate_id, evaluation_sha256,
                    candidate_set_sha256, input_authorities_json,
                    payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    evaluation.evaluation_id,
                    evaluation.document_id,
                    evaluation.scene_revision_id,
                    evaluation.scene_content_hash,
                    evaluation.search_spec_id,
                    evaluation.search_spec_sha256,
                    evaluation.candidate_id,
                    evaluation.evaluation_sha256,
                    authority.candidate_set_sha256,
                    self._input_authorities_json(authority),
                    evaluation.model_dump_json(),
                    evaluation.created_at_utc,
                ),
            )

    def _validated_evaluation(
        self,
        row: sqlite3.Row,
        scans: dict[str, _CandidateSetScan] | None = None,
    ) -> CadObjectiveEvaluation:
        """Deserialize one persisted evaluation and replay its exact authority."""

        evaluation = CadObjectiveEvaluation.model_validate_json(row['payload_json'])
        if (
            row['evaluation_id'] != evaluation.evaluation_id
            or row['document_id'] != evaluation.document_id
            or row['scene_revision_id'] != evaluation.scene_revision_id
            or row['scene_content_hash'] != evaluation.scene_content_hash
            or row['search_spec_id'] != evaluation.search_spec_id
            or row['search_spec_sha256'] != evaluation.search_spec_sha256
            or row['candidate_id'] != evaluation.candidate_id
            or row['evaluation_sha256'] != evaluation.evaluation_sha256
            or row['created_at_utc'] != evaluation.created_at_utc
        ):
            raise ValueError(
                'persisted CadObjectiveEvaluation row disagrees with its payload'
            )
        if (
            row['candidate_set_sha256'] is None
            or row['input_authorities_json'] is None
        ):
            # Backward-compatibility policy: rows written before O30 evidence
            # authority existed were never replay-attested, so they are
            # non-authoritative and fail closed on read rather than acquiring
            # fabricated current provenance.
            raise ValueError(
                'objective evaluation predates authority attestation and is '
                'non-authoritative'
            )
        authority = self._require_evaluation_authority(evaluation, scans=scans)
        if row['candidate_set_sha256'] != authority.candidate_set_sha256:
            raise ValueError(
                'persisted objective evaluation candidate-set authority mismatch'
            )
        if row['input_authorities_json'] != self._input_authorities_json(authority):
            raise ValueError(
                'persisted objective evaluation input authority mismatch'
            )
        return evaluation

    def get_evaluation(self, evaluation_id: str) -> CadObjectiveEvaluation | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_objective_evaluations WHERE evaluation_id=?',
                (evaluation_id,),
            ).fetchone()
        return None if row is None else self._validated_evaluation(row)

    def inspect_evaluation(self, evaluation_id: str) -> CadObjectiveEvaluation | None:
        """Return the stored payload without authority replay.

        Diagnostic-only view for legacy or suspect rows: the returned model is
        payload-consistent but its evidence binding and vector have NOT been
        re-attested, so it must never feed O40 or other authority consumers.
        """

        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_objective_evaluations WHERE evaluation_id=?',
                (evaluation_id,),
            ).fetchone()
        return (
            None
            if row is None
            else CadObjectiveEvaluation.model_validate_json(row['payload_json'])
        )

    def list_evaluations(self, search_spec_id: str) -> tuple[CadObjectiveEvaluation, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM cad_objective_evaluations WHERE search_spec_id=? ORDER BY seq ASC',
                (search_spec_id,),
            ).fetchall()
        scans: dict[str, _CandidateSetScan] = {}
        return tuple(
            self._validated_evaluation(row, scans) for row in rows
        )

    def _require_pareto_authority(self, pareto_set: CadParetoSet) -> None:
        """Replay the exact authority one Pareto set is bound to.

        Revalidates the SceneRevision/SearchSpec binding, reloads every
        referenced O30 evaluation through the evaluation read path,
        verifies exact evaluation SHA/candidate/binding, and requires the
        canonical recomputed front to equal the submitted result. Missing
        or mismatched authority fails closed; used by both save-time
        validation and authoritative reads.
        """

        self._resolve_binding(
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
