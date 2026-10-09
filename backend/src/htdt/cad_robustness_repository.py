from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any, NamedTuple

from .cad_constraint_models import CadConstraintSet
from .cad_constraints import evaluate_cad_constraints
from .cad_extended_search import generate_extended_candidates
from .cad_extended_search_repository import CadExtendedSearchRepository
from .cad_objective_models import CadObjectiveEvaluation
from .cad_objective_repository import CadObjectiveRepository
from .cad_orientation_constraints import orientation_constraint_rejections
from .cad_repository import SceneRepository, SceneRevision
from .cad_scene import scene_content_hash
from .cad_search import generate_cad_candidates
from .cad_search_models import CadSearchSpec
from .cad_search_repository import CadSearchRepository
from .cad_schema import (
    connect_sqlite,
    ensure_native_schema,
    require_native_tables,
)
from .optimization_robustness import (
    LocalPerturbation,
    PerturbationSample,
    RobustnessCandidate,
    RobustnessEvaluation,
    RobustnessSpec,
    _candidate_document,
    _candidate_from_payload,
    _copy_nominal_vector,
    _domain_rejections,
    _metric_schema,
    apply_local_perturbation,
    build_local_stencil,
    build_robustness_evaluations,
    build_robustness_spec,
)
from .optimization_robustness_multidimensional import (
    MULTIDIMENSIONAL_SAMPLING_STRATEGY,
    build_multidimensional_robustness_evaluations,
    build_multidimensional_sampling_plan,
    derive_multidimensional_robustness_spec,
)
from .optimization_robustness_uncertainty import (
    UNCERTAINTY_SAMPLING_STRATEGY,
    build_uncertainty_robustness_evaluations,
    build_uncertainty_sampling_plan,
    derive_uncertainty_robustness_spec,
)


class _SpecAuthorities(NamedTuple):
    """Exact upstream authorities a persisted spec was re-derived against."""

    source_revision: SceneRevision
    search_spec: CadSearchSpec
    nominal_objective: CadObjectiveEvaluation
    candidate: RobustnessCandidate
    constraint_set: CadConstraintSet


class _ExpectedSampleEvidence(NamedTuple):
    """Deterministic scene/constraint evidence replayed for one plan item."""

    perturbed_scene_content_hash: str
    feasible: bool
    g10_results: tuple[Any, ...]
    o80_rejection_ids: tuple[str, ...]
    domain_rejection_ids: tuple[str, ...]
    perturbation_failure_reason: str | None


class CadRobustnessRepository:
    """Append-only SQLite persistence for O90 robustness authority and evidence.

    Pydantic validity and self-hashes are necessary but not sufficient for a
    persisted O90 row. Every write and every authoritative read re-establishes
    the upstream authority chain and the canonical derivations the trusted
    builders/evaluators enforce:

    * ``save_spec``/``get_spec``/``list_specs_for_candidate`` re-resolve the
      exact ``SceneRevision`` + ``CadSearchSpec`` + nominal O30
      ``CadObjectiveEvaluation``, replay candidate-set generation to prove the
      persisted candidate payload is a member of the claimed set, then rebuild
      the spec (O90B rows additionally re-derive from their persisted local
      parent) and require exact equality;
    * ``save_sample``/``list_samples`` rebuild the deterministic sampling plan
      from the validated spec, require each stored row to equal the plan member
      at its ``sample_index``, and replay G10/O80/domain evidence against the
      regenerated perturbed scene;
    * ``save_evaluation``/``list_evaluations`` load the complete validated
      sample evidence and require each stored evaluation to equal the
      canonical builder output for its ``objective_id``.

    ``schema_version``/``algorithm_version`` are part of every spec identity
    and are re-verified under the same canonical semantics on every read;
    historical rows are re-validated against the versions they claim and fail
    closed instead of being silently trusted or migrated. External solver
    results stay immutable references: they are checked for identity/binding
    (nominal ref membership in the persisted O30 inputs) and are never
    recomputed.
    """

    def __init__(
        self,
        *,
        scene_repository: SceneRepository,
        search_repository: CadSearchRepository,
        objective_repository: CadObjectiveRepository,
        extended_search_repository: CadExtendedSearchRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.search_repository = search_repository
        self.objective_repository = objective_repository
        self.extended_search_repository = extended_search_repository
        self.db_path = Path(scene_repository.path)
        for label, repository in (
            ('SearchSpec', search_repository),
            ('objective', objective_repository),
            ('extended search', extended_search_repository),
        ):
            if repository is not None and Path(repository.path) != self.db_path:
                raise ValueError(
                    f'O90 robustness and {label} repositories must share '
                    'one native CAD database'
                )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        ensure_native_schema(self.db_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.db_path)
        return connect_sqlite(self.db_path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_robustness_specs', 'cad_perturbation_samples', 'cad_robustness_evaluations')

    @staticmethod
    def _payload(model: Any) -> str:
        return json.dumps(
            model.model_dump(mode='json'),
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        )

    # ------------------------------------------------------------------
    # RobustnessSpec authority
    # ------------------------------------------------------------------

    def _resolve_spec_authorities(
        self,
        spec: RobustnessSpec,
    ) -> _SpecAuthorities:
        """Re-resolve the exact upstream authorities a spec claims."""
        source_revision = self.scene_repository.get(spec.scene_revision_id)
        if source_revision is None:
            raise ValueError('robustness SceneRevision authority does not exist')
        if (
            source_revision.document_id != spec.document_id
            or source_revision.content_hash != spec.scene_content_hash
        ):
            raise ValueError('robustness SceneRevision authority mismatch')

        search_spec = self.search_repository.get(spec.search_spec_id)
        if search_spec is None:
            raise ValueError('robustness SearchSpec authority does not exist')
        if (
            search_spec.document_id != spec.document_id
            or search_spec.scene_revision_id != spec.scene_revision_id
            or search_spec.scene_content_hash != spec.scene_content_hash
            or search_spec.search_spec_sha256 != spec.search_spec_sha256
        ):
            raise ValueError('robustness SearchSpec authority mismatch')

        nominal = self.objective_repository.get_evaluation(
            spec.nominal_objective_evaluation_id
        )
        if nominal is None:
            raise ValueError('robustness nominal O30 evaluation does not exist')
        if (
            nominal.document_id != spec.document_id
            or nominal.scene_revision_id != spec.scene_revision_id
            or nominal.scene_content_hash != spec.scene_content_hash
            or nominal.search_spec_id != spec.search_spec_id
            or nominal.search_spec_sha256 != spec.search_spec_sha256
            or nominal.candidate_id != spec.candidate_id
            or nominal.evaluation_sha256
            != spec.nominal_objective_evaluation_sha256
            or nominal.evaluation_spec_sha256
            != spec.objective_evaluation_spec_sha256
        ):
            raise ValueError(
                'robustness nominal O30 evaluation authority mismatch'
            )
        if not any(
            ref.source_id == spec.nominal_prediction_result_ref
            for ref in nominal.input_refs
        ):
            raise ValueError(
                'robustness nominal prediction ref is not a persisted '
                'O30 input authority'
            )

        candidate = _candidate_from_payload(
            spec.candidate_kind,
            json.loads(spec.candidate_payload_json),
        )
        self._require_candidate_membership(spec, search_spec, candidate)
        constraint_set = CadConstraintSet.model_validate(
            json.loads(search_spec.constraint_snapshot_json)
        )
        return _SpecAuthorities(
            source_revision=source_revision,
            search_spec=search_spec,
            nominal_objective=nominal,
            candidate=candidate,
            constraint_set=constraint_set,
        )

    def _require_candidate_membership(
        self,
        spec: RobustnessSpec,
        search_spec: CadSearchSpec,
        candidate: RobustnessCandidate,
    ) -> None:
        """Replay candidate-set generation and require exact membership.

        A fabricated candidate payload cannot become O90 authority: the
        persisted ``candidate_set_sha256`` must be reproduced by the canonical
        generator and the member at ``candidate.feasible_index`` must equal the
        persisted payload exactly. Extended candidates resolve their persisted
        ``CadExtendedSearchSpec`` from the same native database and replay the
        extended generator; without extended-search authority they fail closed.
        """
        if spec.candidate_kind == 'extended_candidate':
            repository = self.extended_search_repository
            if repository is None:
                raise ValueError(
                    'extended robustness candidate requires persisted '
                    'extended search authority'
                )
            for extended_spec in repository.list_for_base_search(
                spec.search_spec_id
            ):
                page = generate_extended_candidates(
                    self.scene_repository,
                    search_spec,
                    extended_spec,
                    offset=candidate.feasible_index,
                    limit=1,
                )
                if page.candidate_set_sha256 != spec.candidate_set_sha256:
                    continue
                if page.candidates != (candidate,):
                    raise ValueError(
                        'robustness extended candidate is not the persisted '
                        'candidate-set member'
                    )
                return
            raise ValueError(
                'robustness candidate set does not match any persisted '
                'extended search authority'
            )
        page = generate_cad_candidates(
            self.scene_repository,
            search_spec,
            offset=candidate.feasible_index,
            limit=1,
        )
        if page.candidate_set_sha256 != spec.candidate_set_sha256:
            raise ValueError('robustness candidate-set authority mismatch')
        if page.candidates != (candidate,):
            raise ValueError(
                'robustness candidate is not the persisted candidate-set member'
            )

    def _validated_spec(
        self,
        spec: RobustnessSpec,
    ) -> tuple[RobustnessSpec, _SpecAuthorities]:
        """Re-derive one spec from its exact persisted authorities."""
        spec = RobustnessSpec.model_validate(spec.model_dump(mode='python'))
        authorities = self._resolve_spec_authorities(spec)
        if spec.sampling_strategy == 'deterministic_local_stencil':
            rebuilt = build_robustness_spec(
                source_revision=authorities.source_revision,
                search_spec=authorities.search_spec,
                candidate=authorities.candidate,
                candidate_set_sha256=spec.candidate_set_sha256,
                nominal_objective=authorities.nominal_objective,
                nominal_prediction_result_ref=spec.nominal_prediction_result_ref,
                model_id=spec.model_id,
                model_version=spec.model_version,
                prediction_provider_id=spec.prediction_provider_id,
                fidelity=spec.fidelity,
                axes=spec.axes,
                software_version=spec.software_version,
                created_at_utc=spec.created_at_utc,
            )
        else:
            assert spec.parent_robustness_spec_id is not None
            try:
                parent, _parent_authorities = self._persisted_spec(
                    spec.parent_robustness_spec_id
                )
            except KeyError as exc:
                raise ValueError(
                    'derived robustness spec requires a persisted local parent'
                ) from exc
            if (
                parent.robustness_spec_sha256
                != spec.parent_robustness_spec_sha256
            ):
                raise ValueError(
                    'derived robustness spec parent hash mismatch'
                )
            if spec.sampling_strategy == MULTIDIMENSIONAL_SAMPLING_STRATEGY:
                assert spec.sample_count is not None
                assert spec.sampling_seed is not None
                rebuilt = derive_multidimensional_robustness_spec(
                    parent,
                    sample_count=spec.sample_count,
                    seed=spec.sampling_seed,
                    linked_groups=spec.linked_groups,
                    created_at_utc=spec.created_at_utc,
                )
            elif spec.sampling_strategy == UNCERTAINTY_SAMPLING_STRATEGY:
                assert spec.input_uncertainty_model is not None
                rebuilt = derive_uncertainty_robustness_spec(
                    parent,
                    uncertainty_model=spec.input_uncertainty_model,
                    sample_count=spec.sample_count,
                    seed=spec.sampling_seed,
                    created_at_utc=spec.created_at_utc,
                )
            else:
                raise ValueError(
                    f'unsupported robustness sampling strategy: '
                    f'{spec.sampling_strategy}'
                )
        if rebuilt != spec:
            raise ValueError(
                'RobustnessSpec does not reproduce from persisted authorities'
            )
        return spec, authorities

    def _persisted_spec(
        self,
        robustness_spec_id: str,
    ) -> tuple[RobustnessSpec, _SpecAuthorities]:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT *
                FROM cad_robustness_specs
                WHERE robustness_spec_id = ?
                """,
                (robustness_spec_id,),
            ).fetchone()
        if row is None:
            raise KeyError(robustness_spec_id)
        spec = RobustnessSpec.model_validate_json(str(row['payload_json']))
        if (
            row['document_id'] != spec.document_id
            or row['scene_revision_id'] != spec.scene_revision_id
            or row['scene_content_hash'] != spec.scene_content_hash
            or row['search_spec_id'] != spec.search_spec_id
            or row['candidate_id'] != spec.candidate_id
            or row['nominal_objective_evaluation_id']
            != spec.nominal_objective_evaluation_id
            or row['model_id'] != spec.model_id
            or row['model_version'] != spec.model_version
            or row['robustness_spec_sha256'] != spec.robustness_spec_sha256
            or row['created_at_utc'] != spec.created_at_utc
        ):
            raise ValueError(
                'persisted RobustnessSpec row disagrees with its payload'
            )
        return self._validated_spec(spec)

    def save_spec(self, spec: RobustnessSpec) -> RobustnessSpec:
        spec, _authorities = self._validated_spec(spec)
        payload = self._payload(spec)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_robustness_specs
                WHERE robustness_spec_id = ?
                """,
                (spec.robustness_spec_id,),
            ).fetchone()
            if row is not None:
                existing = RobustnessSpec.model_validate_json(
                    str(row['payload_json'])
                )
                if existing.robustness_spec_sha256 != spec.robustness_spec_sha256:
                    raise ValueError(
                        'RobustnessSpec immutable identity conflict'
                    )
                persisted = self._persisted_spec(spec.robustness_spec_id)[0]
                return persisted
            connection.execute(
                """
                INSERT INTO cad_robustness_specs (
                    robustness_spec_id,
                    document_id,
                    scene_revision_id,
                    scene_content_hash,
                    search_spec_id,
                    candidate_id,
                    nominal_objective_evaluation_id,
                    model_id,
                    model_version,
                    payload_json,
                    robustness_spec_sha256,
                    created_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    spec.robustness_spec_id,
                    spec.document_id,
                    spec.scene_revision_id,
                    spec.scene_content_hash,
                    spec.search_spec_id,
                    spec.candidate_id,
                    spec.nominal_objective_evaluation_id,
                    spec.model_id,
                    spec.model_version,
                    payload,
                    spec.robustness_spec_sha256,
                    spec.created_at_utc,
                ),
            )
        return spec

    def get_spec(self, robustness_spec_id: str) -> RobustnessSpec:
        return self._persisted_spec(robustness_spec_id)[0]

    def list_specs_for_candidate(
        self,
        *,
        document_id: str,
        scene_revision_id: str,
        candidate_id: str,
    ) -> tuple[RobustnessSpec, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT *
                FROM cad_robustness_specs
                WHERE document_id = ?
                  AND scene_revision_id = ?
                  AND candidate_id = ?
                ORDER BY created_at_utc ASC, robustness_spec_id ASC
                """,
                (document_id, scene_revision_id, candidate_id),
            ).fetchall()
        return tuple(
            self._validated_spec_row(row)
            for row in rows
        )

    def list_specs_for_search(
        self,
        *,
        document_id: str,
        scene_revision_id: str,
        search_spec_id: str,
    ) -> tuple[RobustnessSpec, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT *
                FROM cad_robustness_specs
                WHERE document_id = ?
                  AND scene_revision_id = ?
                  AND search_spec_id = ?
                ORDER BY created_at_utc ASC, robustness_spec_id ASC
                """,
                (document_id, scene_revision_id, search_spec_id),
            ).fetchall()
        return tuple(
            self._validated_spec_row(row)
            for row in rows
        )

    def _validated_spec_row(self, row: sqlite3.Row) -> RobustnessSpec:
        spec = RobustnessSpec.model_validate_json(str(row['payload_json']))
        if (
            row['document_id'] != spec.document_id
            or row['scene_revision_id'] != spec.scene_revision_id
            or row['scene_content_hash'] != spec.scene_content_hash
            or row['search_spec_id'] != spec.search_spec_id
            or row['candidate_id'] != spec.candidate_id
            or row['nominal_objective_evaluation_id']
            != spec.nominal_objective_evaluation_id
            or row['model_id'] != spec.model_id
            or row['model_version'] != spec.model_version
            or row['robustness_spec_sha256'] != spec.robustness_spec_sha256
            or row['created_at_utc'] != spec.created_at_utc
        ):
            raise ValueError(
                'persisted RobustnessSpec row disagrees with its payload'
            )
        return self._validated_spec(spec)[0]

    # ------------------------------------------------------------------
    # PerturbationSample evidence
    # ------------------------------------------------------------------

    @staticmethod
    def _sampling_plan(spec: RobustnessSpec) -> tuple[LocalPerturbation, ...]:
        if spec.sampling_strategy == 'deterministic_local_stencil':
            return build_local_stencil(spec)
        if spec.sampling_strategy == MULTIDIMENSIONAL_SAMPLING_STRATEGY:
            return build_multidimensional_sampling_plan(spec)
        if spec.sampling_strategy == UNCERTAINTY_SAMPLING_STRATEGY:
            return build_uncertainty_sampling_plan(spec)
        raise ValueError(
            f'unsupported robustness sampling strategy: {spec.sampling_strategy}'
        )

    def _expected_sample_evidence(
        self,
        *,
        spec: RobustnessSpec,
        plan: LocalPerturbation,
        authorities: _SpecAuthorities,
    ) -> _ExpectedSampleEvidence:
        """Replay the deterministic perturbed-scene evidence for one plan item.

        This mirrors ``evaluate_local_robustness`` and the O90B
        ``_multidimensional_sample`` exactly: the same perturbation application
        order, the same domain rejection bookkeeping, the same changed-entity
        set handed to the O80 orientation gate, and the same feasible
        computation over the persisted constraint snapshot.
        """
        axis_by_id = {axis.axis_id: axis for axis in spec.axes}
        document = _candidate_document(
            authorities.source_revision,
            authorities.candidate,
        )
        changed: set[str] = set()
        domain_rejections: list[str] = []
        perturbation_failure_reason: str | None = None

        if spec.sampling_strategy == 'deterministic_local_stencil':
            if plan.axis_id is not None:
                axis = axis_by_id[plan.axis_id]
                delta = float(plan.parameter_deltas[plan.axis_id])
                changed.add(axis.entity_id)
                domain_rejections.extend(_domain_rejections(axis, delta))
                if not domain_rejections:
                    try:
                        document = apply_local_perturbation(document, axis, delta)
                    except Exception as exc:  # error-boundary: per-sample perturbation — any failure type is sealed as the sample's unsupported-domain rejection and perturbation_failed reason, never aborting the stencil (noqa: BLE001)
                        domain_rejections.append(
                            f'__perturbation_unsupported__:{axis.axis_id}'
                        )
                        perturbation_failure_reason = (
                            f'perturbation_failed:{exc}'
                        )
            expected_domain_ids = tuple(domain_rejections)
        else:
            for axis_id in sorted(plan.parameter_deltas):
                axis = axis_by_id[axis_id]
                delta = float(plan.parameter_deltas[axis_id])
                domain_rejections.extend(_domain_rejections(axis, delta))
                changed.add(axis.entity_id)
                try:
                    document = apply_local_perturbation(document, axis, delta)
                except Exception as exc:  # error-boundary: per-sample perturbation — any failure type is sealed as the sample's unsupported-domain rejection and perturbation_failed reason, never aborting the sweep (noqa: BLE001)
                    domain_rejections.append(
                        f'__perturbation_unsupported__:{axis.axis_id}'
                    )
                    perturbation_failure_reason = f'perturbation_failed:{exc}'
            expected_domain_ids = tuple(sorted(set(domain_rejections)))
        if plan.step == 'nominal':
            changed.update(axis.entity_id for axis in spec.axes)

        g10 = evaluate_cad_constraints(document, authorities.constraint_set)
        o80 = orientation_constraint_rejections(
            document,
            authorities.constraint_set,
            changed_entity_ids=tuple(sorted(changed)),
        )
        feasible = (
            g10.constraints_satisfied
            and not o80
            and not expected_domain_ids
        )
        return _ExpectedSampleEvidence(
            perturbed_scene_content_hash=scene_content_hash(document),
            feasible=feasible,
            g10_results=tuple(g10.results),
            o80_rejection_ids=tuple(o80),
            domain_rejection_ids=expected_domain_ids,
            perturbation_failure_reason=perturbation_failure_reason,
        )

    def _validate_sample(self, sample: PerturbationSample) -> PerturbationSample:
        sample = PerturbationSample.model_validate(
            sample.model_dump(mode='python')
        )
        spec, authorities = self._persisted_spec(sample.robustness_spec_id)
        if (
            sample.robustness_spec_sha256 != spec.robustness_spec_sha256
            or sample.candidate_id != spec.candidate_id
            or sample.model_id != spec.model_id
            or sample.model_version != spec.model_version
            or sample.prediction_provider_id != spec.prediction_provider_id
            or sample.fidelity != spec.fidelity
            or sample.objective_evaluation_spec_sha256
            != spec.objective_evaluation_spec_sha256
        ):
            raise ValueError('PerturbationSample robustness authority mismatch')

        plans = self._sampling_plan(spec)
        expected_plan = LocalPerturbation(
            sample_id=sample.sample_id,
            sample_index=sample.sample_index,
            axis_id=sample.axis_id,
            step=sample.step,
            parameter_deltas=sample.parameter_deltas,
            uncertainty_model_sha256=sample.uncertainty_model_sha256,
            uncertainty_item_id=sample.uncertainty_item_id,
            probability_weight=sample.probability_weight,
        )
        if (
            sample.sample_index >= len(plans)
            or plans[sample.sample_index] != expected_plan
        ):
            raise ValueError(
                'PerturbationSample does not match the deterministic '
                'sampling plan'
            )
        plan = plans[sample.sample_index]
        expected = self._expected_sample_evidence(
            spec=spec,
            plan=plan,
            authorities=authorities,
        )
        if (
            sample.perturbed_scene_content_hash
            != expected.perturbed_scene_content_hash
            or sample.feasible != expected.feasible
            or tuple(sample.g10_results) != expected.g10_results
            or tuple(sample.o80_rejection_ids) != expected.o80_rejection_ids
            or tuple(sample.domain_rejection_ids)
            != expected.domain_rejection_ids
        ):
            raise ValueError(
                'PerturbationSample does not reproduce deterministic '
                'scene evidence'
            )

        nominal = authorities.nominal_objective
        if sample.objective_vector is not None and (
            _metric_schema(sample.objective_vector)
            != _metric_schema(nominal.vector)
        ):
            raise ValueError(
                'PerturbationSample objective schema does not match '
                'the nominal O30 vector'
            )
        if plan.step == 'nominal' and sample.feasible:
            if (
                sample.prediction_result_ref
                != spec.nominal_prediction_result_ref
            ):
                raise ValueError(
                    'PerturbationSample nominal evidence ref mismatch'
                )
            if sample.objective_vector != _copy_nominal_vector(
                nominal,
                sample.sample_id,
            ):
                raise ValueError(
                    'PerturbationSample nominal objective vector mismatch'
                )
        if not sample.feasible:
            expected_failure = (
                expected.perturbation_failure_reason
                or 'hard_constraint_violation'
            )
            if sample.failure_reason != expected_failure:
                raise ValueError('PerturbationSample failure_reason mismatch')
            if sample.objective_vector is not None:
                raise ValueError(
                    'infeasible PerturbationSample must remain unscored'
                )
            if sample.prediction_result_ref is not None:
                raise ValueError(
                    'infeasible PerturbationSample cannot carry '
                    'a prediction ref'
                )
        elif sample.objective_vector is None:
            if not (sample.failure_reason or '').startswith(
                'objective_evaluation_failed:'
            ):
                raise ValueError(
                    'feasible unscored PerturbationSample requires '
                    'evaluator failure provenance'
                )
            if sample.prediction_result_ref is not None:
                raise ValueError(
                    'unscored PerturbationSample cannot carry a prediction ref'
                )
        elif sample.failure_reason is not None:
            raise ValueError(
                'scored PerturbationSample cannot carry failure_reason'
            )
        return sample

    def save_sample(self, sample: PerturbationSample) -> PerturbationSample:
        sample = self._validate_sample(sample)
        payload = self._payload(sample)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_perturbation_samples
                WHERE sample_id = ?
                """,
                (sample.sample_id,),
            ).fetchone()
            if row is not None:
                existing = PerturbationSample.model_validate_json(
                    str(row['payload_json'])
                )
                if existing.sample_sha256 != sample.sample_sha256:
                    raise ValueError(
                        'PerturbationSample immutable identity conflict'
                    )
                return self._validate_sample(existing)
            connection.execute(
                """
                INSERT INTO cad_perturbation_samples (
                    sample_id,
                    robustness_spec_id,
                    candidate_id,
                    sample_index,
                    feasible,
                    payload_json,
                    sample_sha256,
                    created_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sample.sample_id,
                    sample.robustness_spec_id,
                    sample.candidate_id,
                    sample.sample_index,
                    int(sample.feasible),
                    payload,
                    sample.sample_sha256,
                    sample.created_at_utc,
                ),
            )
        return sample

    def save_samples(
        self,
        samples: tuple[PerturbationSample, ...],
    ) -> tuple[PerturbationSample, ...]:
        for sample in samples:
            self.save_sample(sample)
        return samples

    def list_samples(
        self,
        robustness_spec_id: str,
    ) -> tuple[PerturbationSample, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT *
                FROM cad_perturbation_samples
                WHERE robustness_spec_id = ?
                ORDER BY sample_index ASC
                """,
                (robustness_spec_id,),
            ).fetchall()
        return tuple(
            self._validated_sample_row(row)
            for row in rows
        )

    def _validated_sample_row(self, row: sqlite3.Row) -> PerturbationSample:
        sample = PerturbationSample.model_validate_json(str(row['payload_json']))
        if (
            row['sample_id'] != sample.sample_id
            or row['robustness_spec_id'] != sample.robustness_spec_id
            or row['candidate_id'] != sample.candidate_id
            or row['sample_index'] != sample.sample_index
            or bool(row['feasible']) != sample.feasible
            or row['sample_sha256'] != sample.sample_sha256
            or row['created_at_utc'] != sample.created_at_utc
        ):
            raise ValueError(
                'persisted PerturbationSample row disagrees with its payload'
            )
        return self._validate_sample(sample)

    def list_reusable_samples(
        self,
        spec: RobustnessSpec,
    ) -> tuple[PerturbationSample, ...]:
        """Return cache evidence only for the exact immutable O90 spec."""

        persisted = self.get_spec(spec.robustness_spec_id)
        if persisted.robustness_spec_sha256 != spec.robustness_spec_sha256:
            raise ValueError('robustness cache spec identity mismatch')
        samples = self.list_samples(spec.robustness_spec_id)
        if any(
            sample.robustness_spec_sha256 != spec.robustness_spec_sha256
            or sample.candidate_id != spec.candidate_id
            or sample.model_id != spec.model_id
            or sample.model_version != spec.model_version
            or sample.prediction_provider_id != spec.prediction_provider_id
            or sample.objective_evaluation_spec_sha256
            != spec.objective_evaluation_spec_sha256
            for sample in samples
        ):
            raise ValueError('robustness cache contains stale sample evidence')
        return samples

    # ------------------------------------------------------------------
    # RobustnessEvaluation evidence
    # ------------------------------------------------------------------

    def _evidence_for_spec(
        self,
        robustness_spec_id: str,
        evidence: dict[str, tuple[RobustnessSpec, tuple[PerturbationSample, ...]]],
    ) -> tuple[RobustnessSpec, tuple[PerturbationSample, ...]]:
        """Spec + full sample listing, loaded once per spec per batch.

        ``_validate_evaluation`` replays every persisted sample for each
        evaluation it checks; batch callers share ``evidence`` so the
        replay runs once per RobustnessSpec instead of once per row.
        """
        cached = evidence.get(robustness_spec_id)
        if cached is None:
            spec, _authorities = self._persisted_spec(robustness_spec_id)
            cached = (spec, self.list_samples(spec.robustness_spec_id))
            evidence[robustness_spec_id] = cached
        return cached

    def _validate_evaluation(
        self,
        evaluation: RobustnessEvaluation,
        *,
        evidence: dict[
            str, tuple[RobustnessSpec, tuple[PerturbationSample, ...]]
        ] | None = None,
    ) -> RobustnessEvaluation:
        evaluation = RobustnessEvaluation.model_validate(
            evaluation.model_dump(mode='python')
        )
        spec, samples = self._evidence_for_spec(
            evaluation.robustness_spec_id,
            {} if evidence is None else evidence,
        )
        if (
            evaluation.robustness_spec_sha256 != spec.robustness_spec_sha256
            or evaluation.candidate_id != spec.candidate_id
        ):
            raise ValueError(
                'RobustnessEvaluation robustness authority mismatch'
            )
        if spec.sampling_strategy == 'deterministic_local_stencil':
            regenerated = build_robustness_evaluations(
                spec,
                samples,
                created_at_utc=evaluation.created_at_utc,
            )
        elif spec.sampling_strategy == MULTIDIMENSIONAL_SAMPLING_STRATEGY:
            regenerated = build_multidimensional_robustness_evaluations(
                spec,
                samples,
                created_at_utc=evaluation.created_at_utc,
            )
        else:
            regenerated = build_uncertainty_robustness_evaluations(
                spec,
                samples,
                created_at_utc=evaluation.created_at_utc,
            )
        match = next(
            (
                item
                for item in regenerated
                if item.objective_id == evaluation.objective_id
            ),
            None,
        )
        if match != evaluation:
            raise ValueError(
                'RobustnessEvaluation does not reproduce from persisted '
                'sample evidence'
            )
        return evaluation

    def save_evaluation(
        self,
        evaluation: RobustnessEvaluation,
    ) -> RobustnessEvaluation:
        with closing(self._connect()) as connection:
            return self._save_evaluation(connection, evaluation)

    def _save_evaluation(
        self,
        connection: sqlite3.Connection,
        evaluation: RobustnessEvaluation,
        *,
        evidence: dict[
            str, tuple[RobustnessSpec, tuple[PerturbationSample, ...]]
        ] | None = None,
    ) -> RobustnessEvaluation:
        evaluation = self._validate_evaluation(evaluation, evidence=evidence)
        payload = self._payload(evaluation)
        with connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_robustness_evaluations
                WHERE evaluation_id = ?
                """,
                (evaluation.evaluation_id,),
            ).fetchone()
            if row is not None:
                existing = RobustnessEvaluation.model_validate_json(
                    str(row['payload_json'])
                )
                if existing.evaluation_sha256 != evaluation.evaluation_sha256:
                    raise ValueError(
                        'RobustnessEvaluation immutable identity conflict'
                    )
                return self._validate_evaluation(existing, evidence=evidence)
            connection.execute(
                """
                INSERT INTO cad_robustness_evaluations (
                    evaluation_id,
                    robustness_spec_id,
                    candidate_id,
                    objective_id,
                    payload_json,
                    evaluation_sha256,
                    created_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.robustness_spec_id,
                    evaluation.candidate_id,
                    evaluation.objective_id,
                    payload,
                    evaluation.evaluation_sha256,
                    evaluation.created_at_utc,
                ),
            )
        return evaluation

    def save_evaluations(
        self,
        evaluations: tuple[RobustnessEvaluation, ...],
    ) -> tuple[RobustnessEvaluation, ...]:
        """Persist a batch over one connection and one evidence replay.

        Each evaluation still commits independently — a mid-batch failure
        leaves earlier items persisted exactly as sequential
        ``save_evaluation`` calls would — but the batch shares one
        connection and one spec/sample evidence listing per RobustnessSpec
        instead of re-reading both per row.
        """
        evidence: dict[
            str, tuple[RobustnessSpec, tuple[PerturbationSample, ...]]
        ] = {}
        with closing(self._connect()) as connection:
            for evaluation in evaluations:
                self._save_evaluation(connection, evaluation, evidence=evidence)
        return evaluations

    def list_evaluations(
        self,
        robustness_spec_id: str,
    ) -> tuple[RobustnessEvaluation, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT *
                FROM cad_robustness_evaluations
                WHERE robustness_spec_id = ?
                ORDER BY objective_id ASC
                """,
                (robustness_spec_id,),
            ).fetchall()
        return tuple(
            self._validated_evaluation_row(row)
            for row in rows
        )

    def _validated_evaluation_row(
        self,
        row: sqlite3.Row,
    ) -> RobustnessEvaluation:
        evaluation = RobustnessEvaluation.model_validate_json(
            str(row['payload_json'])
        )
        if (
            row['evaluation_id'] != evaluation.evaluation_id
            or row['robustness_spec_id'] != evaluation.robustness_spec_id
            or row['candidate_id'] != evaluation.candidate_id
            or row['objective_id'] != evaluation.objective_id
            or row['evaluation_sha256'] != evaluation.evaluation_sha256
            or row['created_at_utc'] != evaluation.created_at_utc
        ):
            raise ValueError(
                'persisted RobustnessEvaluation row disagrees with its payload'
            )
        return self._validate_evaluation(evaluation)
