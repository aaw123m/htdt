from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterable, Iterator, Mapping, NamedTuple

from ..domain.cad_objective_authority import (
    DECLARED_ONLY_EVIDENCE_CLASSES,
    OBJECTIVE_INPUT_RESOLVERS,
    OBJECTIVE_VECTOR_EVALUATORS,
    ObjectiveAuthorityContext,
    ResolvedObjectiveInput,
    objective_spec_authority,
)
from ..domain.cad_objective_models import (
    CadObjectiveEvaluation,
    CadObjectiveInputRef,
    CadParetoSet,
    canonical_objective_json,
)
from ...cad_repository import SceneRepository, SceneRevision
from ...cad_search import iter_cad_candidate_pages
from ...cad_search_models import CadCandidate, CadCandidateSetPage, CadSearchSpec
from ...cad_search_repository import CadSearchRepository
from ..domain.optimization_objectives import ObjectiveVector
from ..domain.pareto import pareto_front
from ...cad_schema import require_native_tables, connect_sqlite


class _CandidateSetScan:
    """Incremental replay memo for one SearchSpec's canonical candidate set.

    Membership replay pages through ``iter_cad_candidate_pages`` once per
    SearchSpec; ``list_evaluations`` shares one scan across rows so a column
    of evaluations does not regenerate the set per row. A page generator
    that raised once would keep raising the same failure on a fresh replay,
    so the scan stores the exception and re-raises it for every later
    membership lookup — a shared scan reports the identical error a fresh
    scan would.
    """

    __slots__ = ('candidate_set_sha256', 'members', 'pages', 'exhausted', 'error')

    def __init__(self, pages: Iterator[CadCandidateSetPage]) -> None:
        self.candidate_set_sha256: str | None = None
        self.members: dict[str, CadCandidate] = {}
        self.pages = pages
        self.exhausted = False
        self.error: BaseException | None = None


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
        return connect_sqlite(self.path)

    def _measurements(self) -> Any:
        if self._measurement_repository is None:
            from ...measurement.persistence.cad_measurement_repository import CadMeasurementRepository

            self._measurement_repository = CadMeasurementRepository(
                self.scene_repository
            )
        return self._measurement_repository

    def _roomsim(self) -> Any:
        if self._roomsim_repository is None:
            from ...cad_roomsim_repository import CadRoomSimRepository

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
            if scan.candidate_set_sha256 is None:
                raise RuntimeError(
                    'objective scan members lack a candidate set hash'
                )
            return candidate, scan.candidate_set_sha256
        if scan.error is not None:
            raise scan.error

        while not scan.exhausted:
            try:
                page = next(scan.pages, None)
            except Exception as exc:  # error-boundary: page-scan — the failure is recorded on the scan so the membership contract can re-raise it identically for every later caller (noqa: BLE001)
                scan.error = exc
                raise
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
        batches: dict[str, Any] | None = None,
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
            roomsim_batches=batches,
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

    def save_evaluation(
        self,
        evaluation: CadObjectiveEvaluation,
        *,
        scans: dict[str, _CandidateSetScan] | None = None,
        batches: dict[str, Any] | None = None,
    ) -> None:
        with closing(self._connect()) as connection:
            self._save_evaluation(connection, evaluation, scans=scans, batches=batches)

    def _save_evaluation(
        self,
        connection: sqlite3.Connection,
        evaluation: CadObjectiveEvaluation,
        *,
        scans: dict[str, _CandidateSetScan] | None,
        batches: dict[str, Any] | None = None,
    ) -> None:
        authority = self._require_evaluation_authority(
            evaluation, scans=scans, batches=batches
        )
        with connection:
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

    def save_evaluations(
        self,
        evaluations: Iterable[CadObjectiveEvaluation],
        *,
        scans: dict[str, _CandidateSetScan] | None = None,
        batches: dict[str, Any] | None = None,
    ) -> None:
        """Persist a batch of evaluations over one connection.

        Each evaluation still commits independently — a mid-batch failure
        leaves earlier items persisted exactly as sequential
        ``save_evaluation`` calls would — but the batch shares one
        connection, one candidate-set replay per SearchSpec, and one
        Room Simulator batch-authority replay per batch_run_id instead of
        regenerating them and re-parsing the schema per row.
        """

        shared = {} if scans is None else scans
        shared_batches: dict[str, Any] = {} if batches is None else batches
        with closing(self._connect()) as connection:
            for evaluation in evaluations:
                self._save_evaluation(
                    connection, evaluation, scans=shared, batches=shared_batches
                )

    def _validated_evaluation(
        self,
        row: sqlite3.Row,
        scans: dict[str, _CandidateSetScan] | None = None,
        batches: dict[str, Any] | None = None,
        validated: dict[tuple[str, str], _EvaluationAuthority] | None = None,
    ) -> CadObjectiveEvaluation:
        """Deserialize one persisted evaluation and replay its exact authority.

        ``validated`` optionally memoizes the replay outcome per sealed
        evaluation identity (``evaluation_id`` + ``evaluation_sha256``) so a
        caller validating the same immutable rows against unchanged persisted
        state — e.g. a view refresh that lists evaluations, resolves a
        Pareto set and saves it — replays the candidate-set/input/vector
        derivation once per distinct evaluation instead of once per call.
        The row/payload/column equality checks still run on every call, so a
        rewritten row never passes on a stale hit.
        """

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
        authority = (
            None
            if validated is None
            else validated.get(
                (evaluation.evaluation_id, evaluation.evaluation_sha256)
            )
        )
        if authority is None:
            authority = self._require_evaluation_authority(
                evaluation, scans=scans, batches=batches
            )
            if validated is not None:
                validated[
                    (evaluation.evaluation_id, evaluation.evaluation_sha256)
                ] = authority
        if row['candidate_set_sha256'] != authority.candidate_set_sha256:
            raise ValueError(
                'persisted objective evaluation candidate-set authority mismatch'
            )
        if row['input_authorities_json'] != self._input_authorities_json(authority):
            raise ValueError(
                'persisted objective evaluation input authority mismatch'
            )
        return evaluation

    def get_evaluation(
        self,
        evaluation_id: str,
        *,
        scans: dict[str, _CandidateSetScan] | None = None,
        batches: dict[str, Any] | None = None,
        validated: dict[tuple[str, str], _EvaluationAuthority] | None = None,
    ) -> CadObjectiveEvaluation | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_objective_evaluations WHERE evaluation_id=?',
                (evaluation_id,),
            ).fetchone()
        return (
            None
            if row is None
            else self._validated_evaluation(row, scans, batches, validated)
        )

    def get_evaluations(
        self,
        evaluation_ids: Iterable[str],
        *,
        scans: dict[str, _CandidateSetScan] | None = None,
        batches: dict[str, Any] | None = None,
        validated: dict[tuple[str, str], _EvaluationAuthority] | None = None,
    ) -> dict[str, CadObjectiveEvaluation | None]:
        """Batch ``get_evaluation`` over one connection and one replay memo.

        Rows are fetched once on a single connection, then each row runs the
        identical ``_validated_evaluation`` authority replay in input order —
        the first invalid row raises exactly as sequential ``get_evaluation``
        calls would, and ids absent from the table map to ``None``. Callers
        validating several evaluations against unchanged persisted state
        share ``scans`` so a SearchSpec's canonical candidate set is
        regenerated once per batch, and ``batches`` so a Room Simulator
        batch-authority replay runs once per batch_run_id, not once per row.
        """

        ids = tuple(dict.fromkeys(evaluation_ids))
        if not ids:
            return {}
        shared = {} if scans is None else scans
        shared_batches: dict[str, Any] = {} if batches is None else batches
        with closing(self._connect()) as connection, connection:
            rows = {
                evaluation_id: connection.execute(
                    'SELECT * FROM cad_objective_evaluations WHERE evaluation_id=?',
                    (evaluation_id,),
                ).fetchone()
                for evaluation_id in ids
            }
        return {
            evaluation_id: (
                None
                if rows[evaluation_id] is None
                else self._validated_evaluation(
                    rows[evaluation_id], shared, shared_batches, validated
                )
            )
            for evaluation_id in ids
        }

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

    def list_evaluations(
        self,
        search_spec_id: str,
        *,
        scans: dict[str, _CandidateSetScan] | None = None,
        batches: dict[str, Any] | None = None,
        validated: dict[tuple[str, str], _EvaluationAuthority] | None = None,
    ) -> tuple[CadObjectiveEvaluation, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM cad_objective_evaluations WHERE search_spec_id=? ORDER BY seq ASC',
                (search_spec_id,),
            ).fetchall()
        shared_scans: dict[str, _CandidateSetScan] = {} if scans is None else scans
        shared_batches: dict[str, Any] = {} if batches is None else batches
        return tuple(
            self._validated_evaluation(row, shared_scans, shared_batches, validated)
            for row in rows
        )

    def count_evaluations(self, search_spec_id: str) -> int:
        """Persisted evaluation count for one search spec.

        Metadata only — unlike ``list_evaluations`` this does not replay
        per-evaluation authority; use it for display counters, never to make
        claims about evaluation payloads.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT COUNT(*) AS evaluation_count '
                'FROM cad_objective_evaluations WHERE search_spec_id=?',
                (search_spec_id,),
            ).fetchone()
        return int(row['evaluation_count'])

    def count_pareto_sets(self, search_spec_id: str) -> int:
        """Persisted Pareto-set count for one search spec.

        Metadata only — unlike ``list_pareto_sets`` this does not replay
        per-set authority (referenced evaluations and the canonical front);
        use it for display counters only.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT COUNT(*) AS pareto_count '
                'FROM cad_pareto_sets WHERE search_spec_id=?',
                (search_spec_id,),
            ).fetchone()
        return int(row['pareto_count'])

    def _require_pareto_authority(
        self,
        pareto_set: CadParetoSet,
        *,
        scans: dict[str, _CandidateSetScan] | None = None,
        batches: dict[str, Any] | None = None,
        validated: dict[tuple[str, str], _EvaluationAuthority] | None = None,
    ) -> None:
        """Replay the exact authority one Pareto set is bound to.

        Revalidates the SceneRevision/SearchSpec binding, reloads every
        referenced O30 evaluation through the evaluation read path,
        verifies exact evaluation SHA/candidate/binding, and requires the
        canonical recomputed front to equal the submitted result. Missing
        or mismatched authority fails closed; used by both save-time
        validation and authoritative reads. Callers resolving several
        Pareto sets over the same unchanged persisted state share the
        ``scans``/``batches``/``validated`` memos so each referenced
        evaluation replays its authority once, not once per set.
        """

        self._resolve_binding(
            pareto_set.document_id,
            pareto_set.scene_revision_id,
            pareto_set.scene_content_hash,
            pareto_set.search_spec_id,
            pareto_set.search_spec_sha256,
        )

        evaluations: list[CadObjectiveEvaluation] = []
        shared_scans: dict[str, _CandidateSetScan] = {} if scans is None else scans
        shared_batches: dict[str, Any] = {} if batches is None else batches
        for ref in pareto_set.evaluations:
            evaluation = self.get_evaluation(
                ref.evaluation_id,
                scans=shared_scans,
                batches=shared_batches,
                validated=validated,
            )
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
            if (
                evaluations
                and evaluation.evaluation_spec_sha256
                != evaluations[0].evaluation_spec_sha256
            ):
                raise ValueError('Pareto objective evaluation spec mismatch')
            evaluations.append(evaluation)

        expected = pareto_front(
            tuple(item.vector for item in evaluations),
            pareto_set.objective_ids,
        )
        if expected != pareto_set.result:
            raise ValueError('Pareto result does not match referenced objective evaluations')

    def _validated_pareto_set(
        self,
        row: sqlite3.Row,
        scans: dict[str, _CandidateSetScan] | None = None,
        batches: dict[str, Any] | None = None,
        validated: dict[tuple[str, str], _EvaluationAuthority] | None = None,
    ) -> CadParetoSet:
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
        self._require_pareto_authority(
            pareto_set, scans=scans, batches=batches, validated=validated
        )
        return pareto_set

    def save_pareto_set(
        self,
        pareto_set: CadParetoSet,
        *,
        scans: dict[str, _CandidateSetScan] | None = None,
        batches: dict[str, Any] | None = None,
        validated: dict[tuple[str, str], _EvaluationAuthority] | None = None,
    ) -> None:
        self._require_pareto_authority(
            pareto_set, scans=scans, batches=batches, validated=validated
        )

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

    def find_pareto_set_by_sha(
        self,
        search_spec_id: str,
        pareto_sha256: str,
        *,
        scans: dict[str, _CandidateSetScan] | None = None,
        batches: dict[str, Any] | None = None,
        validated: dict[tuple[str, str], _EvaluationAuthority] | None = None,
    ) -> CadParetoSet | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_pareto_sets WHERE search_spec_id=? AND pareto_sha256=? '
                'ORDER BY seq DESC LIMIT 1',
                (search_spec_id, pareto_sha256),
            ).fetchone()
        return (
            None
            if row is None
            else self._validated_pareto_set(row, scans, batches, validated)
        )

    def get_pareto_set(
        self,
        pareto_set_id: str,
        *,
        scans: dict[str, _CandidateSetScan] | None = None,
        batches: dict[str, Any] | None = None,
        validated: dict[tuple[str, str], _EvaluationAuthority] | None = None,
    ) -> CadParetoSet | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_pareto_sets WHERE pareto_set_id=?',
                (pareto_set_id,),
            ).fetchone()
        return (
            None
            if row is None
            else self._validated_pareto_set(row, scans, batches, validated)
        )

    def latest_evaluations_by_candidate(
        self,
        search_spec_id: str,
        *,
        scans: dict[str, _CandidateSetScan] | None = None,
        batches: dict[str, Any] | None = None,
        validated: dict[tuple[str, str], _EvaluationAuthority] | None = None,
    ) -> tuple[CadObjectiveEvaluation, ...]:
        """Return the latest immutable evaluation for each candidate, preserving candidate first-seen order."""
        evaluations = self.list_evaluations(
            search_spec_id, scans=scans, batches=batches, validated=validated
        )
        order: list[str] = []
        latest: dict[str, CadObjectiveEvaluation] = {}
        for evaluation in evaluations:
            if evaluation.candidate_id not in latest:
                order.append(evaluation.candidate_id)
            latest[evaluation.candidate_id] = evaluation
        return tuple(latest[candidate_id] for candidate_id in order)

    def list_pareto_sets(
        self,
        search_spec_id: str,
        *,
        scans: dict[str, _CandidateSetScan] | None = None,
        batches: dict[str, Any] | None = None,
        validated: dict[tuple[str, str], _EvaluationAuthority] | None = None,
    ) -> tuple[CadParetoSet, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM cad_pareto_sets WHERE search_spec_id=? ORDER BY seq ASC',
                (search_spec_id,),
            ).fetchall()
        return tuple(
            self._validated_pareto_set(row, scans, batches, validated)
            for row in rows
        )
