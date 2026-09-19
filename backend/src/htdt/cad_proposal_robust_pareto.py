from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_proposal_robustness import ProposalRobustnessAuthority
from .cad_repository import SceneRepository
from .cad_schema import ensure_native_schema
from .cad_topology_comparison import (
    TopologyComparisonEvaluation,
    VariantEvaluationBundle,
)
from .optimization_robustness import RobustnessEvaluation
from .optimization_robustness_multidimensional import (
    RobustParetoSelection,
    robust_pareto_front,
)
from .pareto import ParetoResult


PROPOSAL_ROBUST_PARETO_SCHEMA_VERSION = 1
PROPOSAL_ROBUST_PARETO_AUTHORITY_VERSION = 'o100f-proposal-robust-pareto-1'


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


class TopologyComparisonResolver(Protocol):
    path: Path

    def get_evaluation(
        self,
        evaluation_id: str,
    ) -> TopologyComparisonEvaluation | None:
        ...

    def get_bundle(
        self,
        bundle_id: str,
    ) -> VariantEvaluationBundle | None:
        ...


class ProposalRobustnessResolver(Protocol):
    path: Path

    def get_spec(
        self,
        robustness_spec_id: str,
    ) -> ProposalRobustnessAuthority | None:
        ...

    def list_evaluations(
        self,
        robustness_spec_id: str,
    ) -> tuple[RobustnessEvaluation, ...]:
        ...


class ProposalRobustnessEvaluationRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    objective_id: str = Field(min_length=1)
    evaluation_id: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    robustness_spec_id: str = Field(min_length=1)
    robustness_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class ProposalRobustCandidateRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    variant_id: str = Field(min_length=1)
    variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    bundle_id: str = Field(min_length=1)
    bundle_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    robustness_spec_id: str = Field(min_length=1)
    robustness_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    robustness_evaluations: tuple[
        ProposalRobustnessEvaluationRef,
        ...,
    ] = Field(min_length=1)

    @model_validator(mode='after')
    def unique_objectives(self) -> 'ProposalRobustCandidateRef':
        objective_ids = [
            item.objective_id for item in self.robustness_evaluations
        ]
        if len(objective_ids) != len(set(objective_ids)):
            raise ValueError(
                'proposal robust candidate evaluation objectives must be unique'
            )
        return self


class ProposalRobustParetoEvaluation(BaseModel):
    """Exact O100F nominal/robust Pareto over the O100D eligible candidate set."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = PROPOSAL_ROBUST_PARETO_SCHEMA_VERSION
    authority_version: Literal[
        'o100f-proposal-robust-pareto-1'
    ] = PROPOSAL_ROBUST_PARETO_AUTHORITY_VERSION

    evaluation_id: str = Field(
        pattern=r'^proposal-robust-pareto:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    topology_comparison_evaluation_id: str = Field(min_length=1)
    topology_comparison_evaluation_sha256: str = Field(
        pattern=r'^[0-9a-f]{64}$'
    )
    selection: RobustParetoSelection
    candidates: tuple[ProposalRobustCandidateRef, ...] = Field(min_length=2)
    pareto_result: ParetoResult
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_identity(self) -> 'ProposalRobustParetoEvaluation':
        candidate_ids = [item.variant_id for item in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError('proposal robust Pareto candidates must be unique')
        if set(self.pareto_result.dominated_by) != set(candidate_ids):
            raise ValueError(
                'proposal robust Pareto result candidate set must equal candidates'
            )
        expected_objectives = (
            *(
                f'nominal::{objective_id}'
                for objective_id in self.selection.nominal_objective_ids
            ),
            *(
                f'robust.sampled_worst::{objective_id}'
                for objective_id in self.selection.robustness_objective_ids
            ),
        )
        if self.pareto_result.objective_ids != expected_objectives:
            raise ValueError(
                'proposal robust Pareto result objective axes mismatch selection'
            )
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('ProposalRobustParetoEvaluation semantic hash mismatch')
        if self.evaluation_id != f'proposal-robust-pareto:{expected}':
            raise ValueError('ProposalRobustParetoEvaluation id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'evaluation_id', 'semantic_sha256'},
        )


def _eligible_variant_ids(
    topology_evaluation: TopologyComparisonEvaluation,
) -> tuple[str, ...]:
    return tuple(
        item.variant_id
        for item in topology_evaluation.eligibility
        if item.state == 'ELIGIBLE'
    )


def build_proposal_robust_pareto_evaluation(
    *,
    topology_evaluation: TopologyComparisonEvaluation,
    bundles: Sequence[VariantEvaluationBundle],
    robustness_specs: Sequence[ProposalRobustnessAuthority],
    robustness_evaluations: Mapping[
        str,
        Sequence[RobustnessEvaluation],
    ],
    selection: RobustParetoSelection,
    created_at_utc: str,
) -> ProposalRobustParetoEvaluation:
    eligible_ids = _eligible_variant_ids(topology_evaluation)
    if len(eligible_ids) < 2:
        raise ValueError(
            'proposal robust Pareto requires at least two O100D eligible candidates'
        )
    if topology_evaluation.pareto_result is None:
        raise ValueError(
            'proposal robust Pareto requires exact O100D eligible comparison'
        )

    selected_objectives = {
        *selection.nominal_objective_ids,
        *selection.robustness_objective_ids,
    }
    unknown = selected_objectives - set(topology_evaluation.pareto_objective_ids)
    if unknown:
        raise ValueError(
            'proposal robust Pareto objectives must be O100D common-compatible: '
            f'{sorted(unknown)}'
        )

    bundle_by_variant = {item.variant_id: item for item in bundles}
    if set(bundle_by_variant) != set(eligible_ids):
        raise ValueError(
            'proposal robust Pareto bundles must equal O100D eligible candidate set'
        )
    topology_bundle_by_variant = {
        item.variant_id: item for item in topology_evaluation.bundles
    }
    for variant_id in eligible_ids:
        bundle = bundle_by_variant[variant_id]
        exact_ref = topology_bundle_by_variant.get(variant_id)
        if exact_ref is None:
            raise ValueError(
                f'O100D eligible variant lacks exact bundle ref: {variant_id}'
            )
        if (
            exact_ref.bundle_id != bundle.bundle_id
            or exact_ref.bundle_sha256 != bundle.bundle_sha256
            or exact_ref.variant_sha256 != bundle.variant_sha256
        ):
            raise ValueError(
                f'proposal robust Pareto bundle identity mismatch: {variant_id}'
            )

    spec_by_variant = {
        item.candidate_variant_id: item for item in robustness_specs
    }
    if set(spec_by_variant) != set(eligible_ids):
        raise ValueError(
            'proposal robust Pareto robustness specs must equal eligible set'
        )
    if set(robustness_evaluations) != set(eligible_ids):
        raise ValueError(
            'proposal robust Pareto robustness evaluation map must equal '
            'O100D eligible candidate set'
        )

    candidate_refs: list[ProposalRobustCandidateRef] = []
    pareto_inputs: list[
        tuple[
            Any,
            Sequence[RobustnessEvaluation],
        ]
    ] = []
    for variant_id in eligible_ids:
        bundle = bundle_by_variant[variant_id]
        spec = spec_by_variant[variant_id]
        if (
            spec.candidate_variant_sha256 != bundle.variant_sha256
            or spec.nominal_bundle_id != bundle.bundle_id
            or spec.nominal_bundle_sha256 != bundle.bundle_sha256
        ):
            raise ValueError(
                f'proposal robust Pareto spec/bundle lineage mismatch: {variant_id}'
            )
        missing_spec_objectives = (
            set(selection.robustness_objective_ids) - set(spec.objective_ids)
        )
        if missing_spec_objectives:
            raise ValueError(
                f'proposal robustness spec does not declare selected objectives: '
                f'{variant_id}/{sorted(missing_spec_objectives)}'
            )

        evaluations = tuple(robustness_evaluations.get(variant_id, ()))
        by_objective = {item.objective_id: item for item in evaluations}
        if len(by_objective) != len(evaluations):
            raise ValueError(
                f'proposal robust Pareto duplicate robustness objective: {variant_id}'
            )
        selected_evaluations: list[RobustnessEvaluation] = []
        evaluation_refs: list[ProposalRobustnessEvaluationRef] = []
        for objective_id in selection.robustness_objective_ids:
            evaluation = by_objective.get(objective_id)
            if evaluation is None:
                raise ValueError(
                    f'missing proposal robustness evaluation: '
                    f'{variant_id}/{objective_id}'
                )
            if (
                evaluation.candidate_id != variant_id
                or evaluation.robustness_spec_id != spec.robustness_spec_id
                or evaluation.robustness_spec_sha256
                != spec.robustness_spec_sha256
            ):
                raise ValueError(
                    f'proposal robustness evaluation authority mismatch: '
                    f'{variant_id}/{objective_id}'
                )
            selected_evaluations.append(evaluation)
            evaluation_refs.append(
                ProposalRobustnessEvaluationRef(
                    objective_id=objective_id,
                    evaluation_id=evaluation.evaluation_id,
                    evaluation_sha256=evaluation.evaluation_sha256,
                    robustness_spec_id=evaluation.robustness_spec_id,
                    robustness_spec_sha256=evaluation.robustness_spec_sha256,
                )
            )

        candidate_refs.append(
            ProposalRobustCandidateRef(
                variant_id=variant_id,
                variant_sha256=bundle.variant_sha256,
                bundle_id=bundle.bundle_id,
                bundle_sha256=bundle.bundle_sha256,
                robustness_spec_id=spec.robustness_spec_id,
                robustness_spec_sha256=spec.robustness_spec_sha256,
                robustness_evaluations=tuple(evaluation_refs),
            )
        )
        pareto_inputs.append(
            (bundle.objective_vector, tuple(selected_evaluations))
        )

    pareto_result = robust_pareto_front(
        tuple(pareto_inputs),
        selection,
    )
    core = {
        'schema_version': PROPOSAL_ROBUST_PARETO_SCHEMA_VERSION,
        'authority_version': PROPOSAL_ROBUST_PARETO_AUTHORITY_VERSION,
        'topology_comparison_evaluation_id': topology_evaluation.evaluation_id,
        'topology_comparison_evaluation_sha256': (
            topology_evaluation.evaluation_sha256
        ),
        'selection': selection.model_dump(mode='json'),
        'candidates': [
            item.model_dump(mode='json') for item in candidate_refs
        ],
        'pareto_result': pareto_result.model_dump(mode='json'),
        'created_at_utc': created_at_utc,
    }
    digest = _digest(core)
    return ProposalRobustParetoEvaluation(
        evaluation_id=f'proposal-robust-pareto:{digest}',
        semantic_sha256=digest,
        topology_comparison_evaluation_id=topology_evaluation.evaluation_id,
        topology_comparison_evaluation_sha256=(
            topology_evaluation.evaluation_sha256
        ),
        selection=selection,
        candidates=tuple(candidate_refs),
        pareto_result=pareto_result,
        created_at_utc=created_at_utc,
    )


class CadProposalRobustParetoRepository:
    """Append-only exact O100F robust-Pareto persistence."""

    def __init__(
        self,
        *,
        scene_repository: SceneRepository,
        topology_comparison_repository: TopologyComparisonResolver,
        proposal_robustness_repository: ProposalRobustnessResolver,
    ) -> None:
        self.scene_repository = scene_repository
        self.topology_comparison_repository = topology_comparison_repository
        self.proposal_robustness_repository = proposal_robustness_repository
        self.path = Path(scene_repository.path)
        for label, repository in (
            ('topology comparison', topology_comparison_repository),
            ('proposal robustness', proposal_robustness_repository),
        ):
            if Path(repository.path) != self.path:
                raise ValueError(
                    f'proposal robust Pareto and {label} repositories must '
                    'share one native CAD database'
                )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cad_proposal_robust_pareto_evaluations (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    evaluation_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    topology_comparison_evaluation_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_proposal_robust_pareto_topology_seq
                    ON cad_proposal_robust_pareto_evaluations(
                        topology_comparison_evaluation_id,
                        seq ASC
                    );
                """
            )

    def _resolve(
        self,
        evaluation: ProposalRobustParetoEvaluation,
    ) -> ProposalRobustParetoEvaluation:
        evaluation = ProposalRobustParetoEvaluation.model_validate(
            evaluation.model_dump(mode='python')
        )
        topology = self.topology_comparison_repository.get_evaluation(
            evaluation.topology_comparison_evaluation_id
        )
        if topology is None:
            raise ValueError(
                'proposal robust Pareto topology comparison disappeared'
            )
        if (
            topology.evaluation_sha256
            != evaluation.topology_comparison_evaluation_sha256
        ):
            raise ValueError(
                'proposal robust Pareto topology comparison hash mismatch'
            )

        bundles: list[VariantEvaluationBundle] = []
        specs: list[ProposalRobustnessAuthority] = []
        robustness_by_variant: dict[
            str,
            tuple[RobustnessEvaluation, ...],
        ] = {}
        for candidate in evaluation.candidates:
            bundle = self.topology_comparison_repository.get_bundle(
                candidate.bundle_id
            )
            if bundle is None:
                raise ValueError(
                    'proposal robust Pareto VariantEvaluationBundle disappeared'
                )
            if (
                bundle.bundle_sha256 != candidate.bundle_sha256
                or bundle.variant_id != candidate.variant_id
                or bundle.variant_sha256 != candidate.variant_sha256
            ):
                raise ValueError(
                    'proposal robust Pareto VariantEvaluationBundle mismatch'
                )
            bundles.append(bundle)

            spec = self.proposal_robustness_repository.get_spec(
                candidate.robustness_spec_id
            )
            if spec is None:
                raise ValueError(
                    'proposal robust Pareto robustness spec disappeared'
                )
            if (
                spec.robustness_spec_sha256
                != candidate.robustness_spec_sha256
                or spec.candidate_variant_id != candidate.variant_id
                or spec.candidate_variant_sha256 != candidate.variant_sha256
            ):
                raise ValueError(
                    'proposal robust Pareto robustness spec mismatch'
                )
            specs.append(spec)

            available = {
                item.evaluation_id: item
                for item in self.proposal_robustness_repository.list_evaluations(
                    spec.robustness_spec_id
                )
            }
            selected: list[RobustnessEvaluation] = []
            for exact_ref in candidate.robustness_evaluations:
                item = available.get(exact_ref.evaluation_id)
                if item is None:
                    raise ValueError(
                        'proposal robust Pareto robustness evaluation disappeared'
                    )
                if (
                    item.evaluation_sha256 != exact_ref.evaluation_sha256
                    or item.objective_id != exact_ref.objective_id
                    or item.robustness_spec_id != exact_ref.robustness_spec_id
                    or item.robustness_spec_sha256
                    != exact_ref.robustness_spec_sha256
                ):
                    raise ValueError(
                        'proposal robust Pareto robustness evaluation mismatch'
                    )
                selected.append(item)
            robustness_by_variant[candidate.variant_id] = tuple(selected)

        regenerated = build_proposal_robust_pareto_evaluation(
            topology_evaluation=topology,
            bundles=bundles,
            robustness_specs=specs,
            robustness_evaluations=robustness_by_variant,
            selection=evaluation.selection,
            created_at_utc=evaluation.created_at_utc,
        )
        if regenerated != evaluation:
            raise ValueError(
                'proposal robust Pareto does not reproduce from exact authorities'
            )
        return evaluation

    def save(
        self,
        evaluation: ProposalRobustParetoEvaluation,
    ) -> ProposalRobustParetoEvaluation:
        evaluation = self._resolve(evaluation)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_proposal_robust_pareto_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation.evaluation_id,),
            ).fetchone()
            if row is not None:
                persisted = ProposalRobustParetoEvaluation.model_validate_json(
                    row['payload_json']
                )
                if persisted != evaluation:
                    raise ValueError(
                        'ProposalRobustParetoEvaluation id exists with '
                        'different semantics'
                    )
                return self._resolve(persisted)
            connection.execute(
                """
                INSERT INTO cad_proposal_robust_pareto_evaluations(
                    evaluation_id,
                    semantic_sha256,
                    topology_comparison_evaluation_id,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.semantic_sha256,
                    evaluation.topology_comparison_evaluation_id,
                    evaluation.model_dump_json(),
                    evaluation.created_at_utc,
                ),
            )
        return evaluation

    def get(
        self,
        evaluation_id: str,
    ) -> ProposalRobustParetoEvaluation | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_proposal_robust_pareto_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        return self._resolve(
            ProposalRobustParetoEvaluation.model_validate_json(
                row['payload_json']
            )
        )
