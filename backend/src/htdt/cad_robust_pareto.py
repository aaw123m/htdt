from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_objective_models import CadObjectiveEvaluation
from .cad_schema import ensure_native_schema
from .optimization_robustness import RobustnessEvaluation, RobustnessSpec
from .optimization_robustness_multidimensional import (
    RobustParetoSelection,
    robust_pareto_front,
)
from .pareto import ParetoResult


O90_ROBUST_PARETO_SCHEMA_VERSION = 1
O90_ROBUST_PARETO_AUTHORITY_VERSION = 'o90-robust-pareto-1'


class ObjectiveEvaluationResolver(Protocol):
    path: Path

    def get_evaluation(
        self,
        evaluation_id: str,
    ) -> CadObjectiveEvaluation | None:
        ...


class RobustnessResolver(Protocol):
    def get_spec(self, robustness_spec_id: str) -> RobustnessSpec:
        ...

    def list_evaluations(
        self,
        robustness_spec_id: str,
    ) -> tuple[RobustnessEvaluation, ...]:
        ...


def _repository_path(repository: object) -> Path:
    value = getattr(repository, 'path', None)
    if value is None:
        value = getattr(repository, 'db_path', None)
    if value is None:
        raise ValueError('repository does not expose path/db_path')
    return Path(value)


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


def _axis_signature(spec: RobustnessSpec) -> list[dict[str, Any]]:
    return [
        {
            'axis_id': axis.axis_id,
            'entity_id': axis.entity_id,
            'parameter': axis.parameter,
            'unit': axis.unit,
            'minus_delta': axis.minus_delta,
            'plus_delta': axis.plus_delta,
            'uncertainty_model': axis.uncertainty_model,
            'allowed_min_delta': (
                None
                if axis.allowed_min is None
                else float(axis.allowed_min) - float(axis.nominal_value)
            ),
            'allowed_max_delta': (
                None
                if axis.allowed_max is None
                else float(axis.allowed_max) - float(axis.nominal_value)
            ),
        }
        for axis in spec.axes
    ]


def robustness_comparison_signature(spec: RobustnessSpec) -> dict[str, Any]:
    """Compatibility signature for cross-candidate sampled-robustness comparison."""

    return {
        'model_id': spec.model_id,
        'model_version': spec.model_version,
        'prediction_provider_id': spec.prediction_provider_id,
        'fidelity': spec.fidelity,
        'objective_evaluation_spec_sha256': spec.objective_evaluation_spec_sha256,
        'sampling_strategy': spec.sampling_strategy,
        'algorithm_version': spec.algorithm_version,
        'sample_count': spec.sample_count,
        'axes': _axis_signature(spec),
        'linked_groups': [
            item.model_dump(mode='json') for item in spec.linked_groups
        ],
        'input_uncertainty_model': (
            None
            if spec.input_uncertainty_model is None
            else spec.input_uncertainty_model.model_dump(mode='json')
        ),
    }


class O90RobustnessEvaluationRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    objective_id: str = Field(min_length=1)
    evaluation_id: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class O90RobustParetoCandidateRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    candidate_id: str = Field(min_length=1)
    candidate_kind: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    nominal_objective_evaluation_id: str = Field(min_length=1)
    nominal_objective_evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    robustness_spec_id: str = Field(min_length=1)
    robustness_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    robustness_evaluations: tuple[O90RobustnessEvaluationRef, ...] = Field(
        min_length=1
    )

    @model_validator(mode='after')
    def unique_objectives(self) -> 'O90RobustParetoCandidateRef':
        ids = [item.objective_id for item in self.robustness_evaluations]
        if len(ids) != len(set(ids)):
            raise ValueError('O90 robust Pareto objective refs must be unique')
        return self


class O90RobustParetoEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = O90_ROBUST_PARETO_SCHEMA_VERSION
    authority_version: Literal[
        'o90-robust-pareto-1'
    ] = O90_ROBUST_PARETO_AUTHORITY_VERSION

    evaluation_id: str = Field(pattern=r'^o90-robust-pareto:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    search_spec_id: str = Field(min_length=1)
    search_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    comparison_signature_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    selection: RobustParetoSelection
    candidates: tuple[O90RobustParetoCandidateRef, ...] = Field(min_length=2)
    pareto_result: ParetoResult
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_identity(self) -> 'O90RobustParetoEvaluation':
        ids = [item.candidate_id for item in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError('O90 robust Pareto candidates must be unique')
        if set(self.pareto_result.dominated_by) != set(ids):
            raise ValueError(
                'O90 robust Pareto result candidate set must equal exact candidates'
            )
        expected_axes = (
            *(
                f'nominal::{objective_id}'
                for objective_id in self.selection.nominal_objective_ids
            ),
            *(
                f'robust.sampled_worst::{objective_id}'
                for objective_id in self.selection.robustness_objective_ids
            ),
        )
        if self.pareto_result.objective_ids != expected_axes:
            raise ValueError('O90 robust Pareto objective axes mismatch selection')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('O90RobustParetoEvaluation semantic hash mismatch')
        if self.evaluation_id != f'o90-robust-pareto:{expected}':
            raise ValueError('O90RobustParetoEvaluation id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'evaluation_id', 'semantic_sha256'},
        )


def build_o90_robust_pareto_evaluation(
    *,
    nominal_evaluations: Sequence[CadObjectiveEvaluation],
    robustness_specs: Sequence[RobustnessSpec],
    robustness_evaluations: Mapping[
        str,
        Sequence[RobustnessEvaluation],
    ],
    selection: RobustParetoSelection,
    created_at_utc: str,
) -> O90RobustParetoEvaluation:
    nominal_by_candidate = {
        item.candidate_id: item for item in nominal_evaluations
    }
    if len(nominal_by_candidate) < 2:
        raise ValueError('O90 robust Pareto requires at least two candidates')
    if len(nominal_by_candidate) != len(tuple(nominal_evaluations)):
        raise ValueError('O90 robust Pareto nominal candidates must be unique')

    candidate_ids = tuple(sorted(nominal_by_candidate))
    spec_by_candidate = {item.candidate_id: item for item in robustness_specs}
    if set(spec_by_candidate) != set(candidate_ids):
        raise ValueError(
            'O90 robust Pareto robustness specs must equal nominal candidate set'
        )
    if set(robustness_evaluations) != set(candidate_ids):
        raise ValueError(
            'O90 robust Pareto robustness evaluation map must equal candidate set'
        )

    first_nominal = nominal_by_candidate[candidate_ids[0]]
    first_spec = spec_by_candidate[candidate_ids[0]]
    comparison_signature = robustness_comparison_signature(first_spec)
    comparison_signature_sha = _digest(comparison_signature)

    candidate_refs: list[O90RobustParetoCandidateRef] = []
    pareto_inputs: list[
        tuple[Any, Sequence[RobustnessEvaluation]]
    ] = []

    for candidate_id in candidate_ids:
        nominal = nominal_by_candidate[candidate_id]
        spec = spec_by_candidate[candidate_id]
        if (
            nominal.document_id != first_nominal.document_id
            or nominal.scene_revision_id != first_nominal.scene_revision_id
            or nominal.scene_content_hash != first_nominal.scene_content_hash
            or nominal.search_spec_id != first_nominal.search_spec_id
            or nominal.search_spec_sha256 != first_nominal.search_spec_sha256
        ):
            raise ValueError(
                'O90 robust Pareto nominal evaluations must share exact '
                'SceneRevision/SearchSpec authority'
            )
        if (
            spec.document_id != nominal.document_id
            or spec.scene_revision_id != nominal.scene_revision_id
            or spec.scene_content_hash != nominal.scene_content_hash
            or spec.search_spec_id != nominal.search_spec_id
            or spec.search_spec_sha256 != nominal.search_spec_sha256
            or spec.nominal_objective_evaluation_id != nominal.evaluation_id
            or spec.nominal_objective_evaluation_sha256
            != nominal.evaluation_sha256
            or spec.objective_evaluation_spec_sha256
            != nominal.evaluation_spec_sha256
        ):
            raise ValueError(
                f'O90 robust Pareto spec/nominal authority mismatch: {candidate_id}'
            )
        if spec.candidate_set_sha256 != first_spec.candidate_set_sha256:
            raise ValueError(
                'O90 robust Pareto candidates must share exact candidate set'
            )
        if robustness_comparison_signature(spec) != comparison_signature:
            raise ValueError(
                f'O90 robustness comparison signature mismatch: {candidate_id}'
            )

        available = tuple(robustness_evaluations[candidate_id])
        by_objective = {item.objective_id: item for item in available}
        if len(by_objective) != len(available):
            raise ValueError(
                f'O90 robust Pareto duplicate objective evaluation: {candidate_id}'
            )
        selected: list[RobustnessEvaluation] = []
        refs: list[O90RobustnessEvaluationRef] = []
        for objective_id in selection.robustness_objective_ids:
            evaluation = by_objective.get(objective_id)
            if evaluation is None:
                raise ValueError(
                    f'missing O90 robustness evaluation: '
                    f'{candidate_id}/{objective_id}'
                )
            if (
                evaluation.candidate_id != candidate_id
                or evaluation.robustness_spec_id != spec.robustness_spec_id
                or evaluation.robustness_spec_sha256
                != spec.robustness_spec_sha256
            ):
                raise ValueError(
                    f'O90 robustness evaluation authority mismatch: '
                    f'{candidate_id}/{objective_id}'
                )
            selected.append(evaluation)
            refs.append(
                O90RobustnessEvaluationRef(
                    objective_id=objective_id,
                    evaluation_id=evaluation.evaluation_id,
                    evaluation_sha256=evaluation.evaluation_sha256,
                )
            )

        candidate_refs.append(
            O90RobustParetoCandidateRef(
                candidate_id=candidate_id,
                candidate_kind=spec.candidate_kind,
                candidate_sha256=spec.candidate_sha256,
                nominal_objective_evaluation_id=nominal.evaluation_id,
                nominal_objective_evaluation_sha256=nominal.evaluation_sha256,
                robustness_spec_id=spec.robustness_spec_id,
                robustness_spec_sha256=spec.robustness_spec_sha256,
                robustness_evaluations=tuple(refs),
            )
        )
        pareto_inputs.append((nominal.vector, tuple(selected)))

    pareto_result = robust_pareto_front(
        tuple(pareto_inputs),
        selection,
    )
    core = {
        'schema_version': O90_ROBUST_PARETO_SCHEMA_VERSION,
        'authority_version': O90_ROBUST_PARETO_AUTHORITY_VERSION,
        'document_id': first_nominal.document_id,
        'scene_revision_id': first_nominal.scene_revision_id,
        'scene_content_hash': first_nominal.scene_content_hash,
        'search_spec_id': first_nominal.search_spec_id,
        'search_spec_sha256': first_nominal.search_spec_sha256,
        'candidate_set_sha256': first_spec.candidate_set_sha256,
        'comparison_signature_sha256': comparison_signature_sha,
        'selection': selection.model_dump(mode='json'),
        'candidates': [item.model_dump(mode='json') for item in candidate_refs],
        'pareto_result': pareto_result.model_dump(mode='json'),
        'created_at_utc': created_at_utc,
    }
    digest = _digest(core)
    return O90RobustParetoEvaluation(
        evaluation_id=f'o90-robust-pareto:{digest}',
        semantic_sha256=digest,
        document_id=first_nominal.document_id,
        scene_revision_id=first_nominal.scene_revision_id,
        scene_content_hash=first_nominal.scene_content_hash,
        search_spec_id=first_nominal.search_spec_id,
        search_spec_sha256=first_nominal.search_spec_sha256,
        candidate_set_sha256=first_spec.candidate_set_sha256,
        comparison_signature_sha256=comparison_signature_sha,
        selection=selection,
        candidates=tuple(candidate_refs),
        pareto_result=pareto_result,
        created_at_utc=created_at_utc,
    )


class CadO90RobustParetoRepository:
    """Append-only exact legacy O90 robust-Pareto persistence."""

    def __init__(
        self,
        *,
        objective_repository: ObjectiveEvaluationResolver,
        robustness_repository: RobustnessResolver,
    ) -> None:
        self.objective_repository = objective_repository
        self.robustness_repository = robustness_repository
        self.path = _repository_path(objective_repository)
        if _repository_path(robustness_repository) != self.path:
            raise ValueError(
                'O90 robust Pareto repositories must share one native CAD database'
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
                CREATE TABLE IF NOT EXISTS cad_o90_robust_pareto_evaluations (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    evaluation_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    search_spec_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_o90_robust_pareto_search_seq
                    ON cad_o90_robust_pareto_evaluations(
                        search_spec_id,
                        seq ASC
                    );
                """
            )

    def _resolve(
        self,
        evaluation: O90RobustParetoEvaluation,
    ) -> O90RobustParetoEvaluation:
        evaluation = O90RobustParetoEvaluation.model_validate(
            evaluation.model_dump(mode='python')
        )
        nominals: list[CadObjectiveEvaluation] = []
        specs: list[RobustnessSpec] = []
        robustness_by_candidate: dict[
            str,
            tuple[RobustnessEvaluation, ...],
        ] = {}

        for candidate in evaluation.candidates:
            nominal = self.objective_repository.get_evaluation(
                candidate.nominal_objective_evaluation_id
            )
            if nominal is None:
                raise ValueError(
                    'O90 robust Pareto nominal objective evaluation disappeared'
                )
            if (
                nominal.evaluation_sha256
                != candidate.nominal_objective_evaluation_sha256
                or nominal.candidate_id != candidate.candidate_id
            ):
                raise ValueError(
                    'O90 robust Pareto nominal objective evaluation mismatch'
                )
            nominals.append(nominal)

            try:
                spec = self.robustness_repository.get_spec(
                    candidate.robustness_spec_id
                )
            except KeyError as exc:
                raise ValueError(
                    'O90 robust Pareto robustness spec disappeared'
                ) from exc
            if (
                spec.robustness_spec_sha256
                != candidate.robustness_spec_sha256
                or spec.candidate_id != candidate.candidate_id
                or spec.candidate_sha256 != candidate.candidate_sha256
                or spec.candidate_kind != candidate.candidate_kind
            ):
                raise ValueError('O90 robust Pareto robustness spec mismatch')
            specs.append(spec)

            available = {
                item.evaluation_id: item
                for item in self.robustness_repository.list_evaluations(
                    spec.robustness_spec_id
                )
            }
            selected: list[RobustnessEvaluation] = []
            for ref in candidate.robustness_evaluations:
                item = available.get(ref.evaluation_id)
                if item is None:
                    raise ValueError(
                        'O90 robust Pareto robustness evaluation disappeared'
                    )
                if (
                    item.evaluation_sha256 != ref.evaluation_sha256
                    or item.objective_id != ref.objective_id
                ):
                    raise ValueError(
                        'O90 robust Pareto robustness evaluation mismatch'
                    )
                selected.append(item)
            robustness_by_candidate[candidate.candidate_id] = tuple(selected)

        regenerated = build_o90_robust_pareto_evaluation(
            nominal_evaluations=nominals,
            robustness_specs=specs,
            robustness_evaluations=robustness_by_candidate,
            selection=evaluation.selection,
            created_at_utc=evaluation.created_at_utc,
        )
        if regenerated != evaluation:
            raise ValueError(
                'O90 robust Pareto does not reproduce from exact authorities'
            )
        return evaluation

    def save(
        self,
        evaluation: O90RobustParetoEvaluation,
    ) -> O90RobustParetoEvaluation:
        evaluation = self._resolve(evaluation)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_o90_robust_pareto_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation.evaluation_id,),
            ).fetchone()
            if row is not None:
                persisted = O90RobustParetoEvaluation.model_validate_json(
                    row['payload_json']
                )
                if persisted != evaluation:
                    raise ValueError(
                        'O90 robust Pareto id exists with different semantics'
                    )
                return self._resolve(persisted)
            connection.execute(
                """
                INSERT INTO cad_o90_robust_pareto_evaluations(
                    evaluation_id,
                    semantic_sha256,
                    search_spec_id,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.semantic_sha256,
                    evaluation.search_spec_id,
                    evaluation.model_dump_json(),
                    evaluation.created_at_utc,
                ),
            )
        return evaluation

    def get(
        self,
        evaluation_id: str,
    ) -> O90RobustParetoEvaluation | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_o90_robust_pareto_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        return self._resolve(
            O90RobustParetoEvaluation.model_validate_json(row['payload_json'])
        )
