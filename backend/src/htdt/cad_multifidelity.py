from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Literal, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_repository import SceneRepository
from .cad_schema import ensure_native_schema
from .cad_topology_comparison import TopologyComparisonEvaluation


MULTIFIDELITY_SCHEMA_VERSION = 1
MULTIFIDELITY_PLAN_AUTHORITY_VERSION = 'multifidelity-plan-1'
MULTIFIDELITY_STAGE_RESULT_VERSION = 'multifidelity-stage-result-1'
MULTIFIDELITY_SCREENING_VERSION = 'multifidelity-screening-1'
MULTIFIDELITY_FINALIZATION_VERSION = 'multifidelity-finalization-1'

MultiFidelityDomain = Literal['o90_robustness', 'o100_topology']
StagePolicy = Literal[
    'hard_gate',
    'validated_screening',
    'shortlist_only',
    'final_common_fidelity',
]
StageDecision = Literal[
    'ADVANCE',
    'PRUNED',
    'DEFERRED_BUDGET',
    'BLOCKED_EVIDENCE',
]
ScreeningState = Literal[
    'READY_FOR_COMMON_FIDELITY',
    'PRELIMINARY_BUDGET',
    'BLOCKED_EVIDENCE',
]
FinalClaimState = Literal[
    'COMPLETE',
    'PRELIMINARY_BUDGET',
    'BLOCKED_EVIDENCE',
]


class TopologyComparisonEvaluationResolver(Protocol):
    path: Path

    def get_evaluation(
        self,
        evaluation_id: str,
    ) -> TopologyComparisonEvaluation | None:
        ...


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MultiFidelityAuthorityRef(BaseModel):
    """Exact opaque authority reference used by the shared O90C/O100E contract."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_kind: str = Field(min_length=1)
    authority_id: str = Field(min_length=1)
    authority_version: str | None = Field(default=None, min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    evaluator_id: str | None = Field(default=None, min_length=1)
    evaluator_version: str | None = Field(default=None, min_length=1)
    model_id: str | None = Field(default=None, min_length=1)
    model_version: str | None = Field(default=None, min_length=1)
    fidelity: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def paired_fields(self) -> 'MultiFidelityAuthorityRef':
        if (self.evaluator_id is None) != (self.evaluator_version is None):
            raise ValueError('evaluator id/version must be supplied together')
        if (self.model_id is None) != (self.model_version is None):
            raise ValueError('model id/version must be supplied together')
        return self

    def key(self) -> tuple[str, str, str | None, str]:
        return (
            self.authority_kind,
            self.authority_id,
            self.authority_version,
            self.semantic_sha256,
        )


class MultiFidelityStageDefinition(BaseModel):
    """One immutable fidelity stage.

    A validated_screening stage may eliminate candidates only when an exact
    screening-relationship authority is attached. shortlist_only may reduce
    compute budget, but its omitted candidates are never called PRUNED.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    stage_id: str = Field(min_length=1)
    order: int = Field(ge=0)
    name: str = Field(min_length=1)
    policy: StagePolicy
    fidelity_label: str = Field(min_length=1)
    evaluator_authority: MultiFidelityAuthorityRef
    validated_screening_relationship_ref: MultiFidelityAuthorityRef | None = None

    @model_validator(mode='after')
    def valid_stage_policy(self) -> 'MultiFidelityStageDefinition':
        if (
            self.policy == 'validated_screening'
            and self.validated_screening_relationship_ref is None
        ):
            raise ValueError(
                'validated screening requires an exact screening relationship authority'
            )
        if (
            self.policy != 'validated_screening'
            and self.validated_screening_relationship_ref is not None
        ):
            raise ValueError(
                'screening relationship authority is only valid for '
                'validated_screening policy'
            )
        return self


class MultiFidelityPlan(BaseModel):
    """Shared staged-fidelity authority for O90C and O100E."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = MULTIFIDELITY_SCHEMA_VERSION
    authority_version: Literal[
        'multifidelity-plan-1'
    ] = MULTIFIDELITY_PLAN_AUTHORITY_VERSION

    plan_id: str = Field(pattern=r'^multifidelity-plan:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    domain: MultiFidelityDomain
    name: str = Field(min_length=1)
    baseline_authority: MultiFidelityAuthorityRef
    candidates: tuple[MultiFidelityAuthorityRef, ...] = Field(min_length=2)
    stages: tuple[MultiFidelityStageDefinition, ...] = Field(min_length=2)

    @model_validator(mode='after')
    def validate_plan(self) -> 'MultiFidelityPlan':
        candidate_keys = [item.key() for item in self.candidates]
        if len(candidate_keys) != len(set(candidate_keys)):
            raise ValueError('multi-fidelity candidates must be unique')

        stage_ids = [item.stage_id for item in self.stages]
        if len(stage_ids) != len(set(stage_ids)):
            raise ValueError('multi-fidelity stage ids must be unique')
        if [item.order for item in self.stages] != list(range(len(self.stages))):
            raise ValueError('multi-fidelity stage order must be contiguous from zero')
        final_stages = [
            item
            for item in self.stages
            if item.policy == 'final_common_fidelity'
        ]
        if len(final_stages) != 1 or final_stages[0] != self.stages[-1]:
            raise ValueError(
                'multi-fidelity plan requires exactly one final_common_fidelity '
                'stage and it must be last'
            )

        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('MultiFidelityPlan semantic hash mismatch')
        if self.plan_id != f'multifidelity-plan:{expected}':
            raise ValueError('MultiFidelityPlan id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'plan_id', 'semantic_sha256'},
        )

    def stage(self, stage_id: str) -> MultiFidelityStageDefinition:
        for stage in self.stages:
            if stage.stage_id == stage_id:
                return stage
        raise KeyError(stage_id)


class MultiFidelityStageOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    candidate: MultiFidelityAuthorityRef
    decision: StageDecision
    evidence_refs: tuple[MultiFidelityAuthorityRef, ...] = ()
    reasons: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_outcome(self) -> 'MultiFidelityStageOutcome':
        if len(self.reasons) != len(set(self.reasons)):
            raise ValueError('multi-fidelity outcome reasons must be unique')
        evidence_keys = [item.key() for item in self.evidence_refs]
        if len(evidence_keys) != len(set(evidence_keys)):
            raise ValueError('multi-fidelity outcome evidence refs must be unique')
        if self.decision == 'ADVANCE' and self.reasons:
            raise ValueError('ADVANCE outcome cannot carry rejection reasons')
        if self.decision != 'ADVANCE' and not self.reasons:
            raise ValueError('non-ADVANCE outcome requires a reason')
        if self.decision == 'PRUNED' and not self.evidence_refs:
            raise ValueError('PRUNED outcome requires exact evidence')
        return self


class MultiFidelityStageResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = MULTIFIDELITY_SCHEMA_VERSION
    authority_version: Literal[
        'multifidelity-stage-result-1'
    ] = MULTIFIDELITY_STAGE_RESULT_VERSION

    result_id: str = Field(pattern=r'^multifidelity-stage-result:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    plan_id: str = Field(pattern=r'^multifidelity-plan:[0-9a-f]{64}$')
    plan_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    stage_id: str = Field(min_length=1)
    input_candidates: tuple[MultiFidelityAuthorityRef, ...] = Field(min_length=1)
    outcomes: tuple[MultiFidelityStageOutcome, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def validate_identity(self) -> 'MultiFidelityStageResult':
        if [item.key() for item in self.input_candidates] != [
            item.candidate.key() for item in self.outcomes
        ]:
            raise ValueError(
                'stage outcomes must preserve exact input candidate order/membership'
            )
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('MultiFidelityStageResult semantic hash mismatch')
        if self.result_id != f'multifidelity-stage-result:{expected}':
            raise ValueError('MultiFidelityStageResult id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'result_id', 'semantic_sha256'},
        )


class MultiFidelityScreeningEvaluation(BaseModel):
    """Exact audit of every pre-final stage."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = MULTIFIDELITY_SCHEMA_VERSION
    authority_version: Literal[
        'multifidelity-screening-1'
    ] = MULTIFIDELITY_SCREENING_VERSION

    evaluation_id: str = Field(pattern=r'^multifidelity-screening:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    plan_id: str = Field(pattern=r'^multifidelity-plan:[0-9a-f]{64}$')
    plan_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    stage_results: tuple[MultiFidelityStageResult, ...] = Field(min_length=1)
    surviving_candidates: tuple[MultiFidelityAuthorityRef, ...]
    state: ScreeningState
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_identity(self) -> 'MultiFidelityScreeningEvaluation':
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('MultiFidelityScreeningEvaluation semantic hash mismatch')
        if self.evaluation_id != f'multifidelity-screening:{expected}':
            raise ValueError('MultiFidelityScreeningEvaluation id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'evaluation_id', 'semantic_sha256'},
        )


class MultiFidelityFinalization(BaseModel):
    """Final common-fidelity comparison binding.

    COMPLETE means the screening lane is complete and the surviving exact
    candidates are the eligible candidate set of the final comparison.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = MULTIFIDELITY_SCHEMA_VERSION
    authority_version: Literal[
        'multifidelity-finalization-1'
    ] = MULTIFIDELITY_FINALIZATION_VERSION

    finalization_id: str = Field(
        pattern=r'^multifidelity-finalization:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    screening_evaluation_id: str = Field(
        pattern=r'^multifidelity-screening:[0-9a-f]{64}$'
    )
    screening_evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    final_stage_id: str = Field(min_length=1)
    final_comparison_ref: MultiFidelityAuthorityRef
    final_candidates: tuple[MultiFidelityAuthorityRef, ...]
    claim_state: FinalClaimState

    @model_validator(mode='after')
    def validate_identity(self) -> 'MultiFidelityFinalization':
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('MultiFidelityFinalization semantic hash mismatch')
        if self.finalization_id != f'multifidelity-finalization:{expected}':
            raise ValueError('MultiFidelityFinalization id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'finalization_id', 'semantic_sha256'},
        )


def build_multifidelity_plan(
    *,
    domain: MultiFidelityDomain,
    name: str,
    baseline_authority: MultiFidelityAuthorityRef,
    candidates: Sequence[MultiFidelityAuthorityRef],
    stages: Sequence[MultiFidelityStageDefinition],
) -> MultiFidelityPlan:
    candidate_tuple = tuple(candidates)
    stage_tuple = tuple(stages)
    core = {
        'schema_version': MULTIFIDELITY_SCHEMA_VERSION,
        'authority_version': MULTIFIDELITY_PLAN_AUTHORITY_VERSION,
        'domain': domain,
        'name': name,
        'baseline_authority': baseline_authority.model_dump(mode='json'),
        'candidates': [item.model_dump(mode='json') for item in candidate_tuple],
        'stages': [item.model_dump(mode='json') for item in stage_tuple],
    }
    digest = _digest(core)
    return MultiFidelityPlan(
        plan_id=f'multifidelity-plan:{digest}',
        semantic_sha256=digest,
        domain=domain,
        name=name,
        baseline_authority=baseline_authority,
        candidates=candidate_tuple,
        stages=stage_tuple,
    )


def build_multifidelity_stage_result(
    *,
    plan: MultiFidelityPlan,
    stage_id: str,
    input_candidates: Sequence[MultiFidelityAuthorityRef],
    outcomes: Sequence[MultiFidelityStageOutcome],
) -> MultiFidelityStageResult:
    stage = plan.stage(stage_id)
    if stage.policy == 'final_common_fidelity':
        raise ValueError(
            'final_common_fidelity is represented by final comparison, '
            'not a screening stage result'
        )
    input_tuple = tuple(input_candidates)
    outcome_tuple = tuple(outcomes)
    if [item.key() for item in input_tuple] != [
        item.candidate.key() for item in outcome_tuple
    ]:
        raise ValueError('stage result requires one ordered outcome per input candidate')

    plan_candidates = {item.key() for item in plan.candidates}
    if any(item.key() not in plan_candidates for item in input_tuple):
        raise ValueError('stage result contains candidate outside plan authority')

    for outcome in outcome_tuple:
        if outcome.decision == 'PRUNED':
            if stage.policy not in {'hard_gate', 'validated_screening'}:
                raise ValueError(
                    'candidate pruning is only authorized by hard_gate or '
                    'validated_screening stage'
                )
            if (
                stage.policy == 'validated_screening'
                and stage.validated_screening_relationship_ref is None
            ):
                raise ValueError(
                    'approximate pruning lacks validated screening relationship'
                )
        if (
            outcome.decision == 'DEFERRED_BUDGET'
            and stage.policy != 'shortlist_only'
        ):
            raise ValueError(
                'budget defer is only valid for shortlist_only stage'
            )

    core = {
        'schema_version': MULTIFIDELITY_SCHEMA_VERSION,
        'authority_version': MULTIFIDELITY_STAGE_RESULT_VERSION,
        'plan_id': plan.plan_id,
        'plan_semantic_sha256': plan.semantic_sha256,
        'stage_id': stage_id,
        'input_candidates': [
            item.model_dump(mode='json') for item in input_tuple
        ],
        'outcomes': [
            item.model_dump(mode='json') for item in outcome_tuple
        ],
    }
    digest = _digest(core)
    return MultiFidelityStageResult(
        result_id=f'multifidelity-stage-result:{digest}',
        semantic_sha256=digest,
        plan_id=plan.plan_id,
        plan_semantic_sha256=plan.semantic_sha256,
        stage_id=stage_id,
        input_candidates=input_tuple,
        outcomes=outcome_tuple,
    )


def evaluate_multifidelity_screening(
    *,
    plan: MultiFidelityPlan,
    stage_results: Sequence[MultiFidelityStageResult],
    created_at_utc: str,
) -> MultiFidelityScreeningEvaluation:
    result_tuple = tuple(stage_results)
    screening_stages = plan.stages[:-1]
    if len(result_tuple) != len(screening_stages):
        raise ValueError(
            'screening evaluation requires one result for every pre-final stage'
        )

    active = tuple(plan.candidates)
    saw_budget_defer = False
    saw_blocked_evidence = False

    for stage, result in zip(screening_stages, result_tuple, strict=True):
        if (
            result.plan_id != plan.plan_id
            or result.plan_semantic_sha256 != plan.semantic_sha256
        ):
            raise ValueError('stage result belongs to another multi-fidelity plan')
        if result.stage_id != stage.stage_id:
            raise ValueError('multi-fidelity stage result order/id mismatch')
        if [item.key() for item in result.input_candidates] != [
            item.key() for item in active
        ]:
            raise ValueError(
                'multi-fidelity stage input is not the exact survivor set '
                'from the preceding stage'
            )

        regenerated = build_multifidelity_stage_result(
            plan=plan,
            stage_id=stage.stage_id,
            input_candidates=active,
            outcomes=result.outcomes,
        )
        if regenerated != result:
            raise ValueError('multi-fidelity stage result does not recompute exactly')

        next_active: list[MultiFidelityAuthorityRef] = []
        for outcome in result.outcomes:
            if outcome.decision == 'ADVANCE':
                next_active.append(outcome.candidate)
            elif outcome.decision == 'DEFERRED_BUDGET':
                saw_budget_defer = True
            elif outcome.decision == 'BLOCKED_EVIDENCE':
                saw_blocked_evidence = True
        active = tuple(next_active)

    if saw_blocked_evidence:
        state: ScreeningState = 'BLOCKED_EVIDENCE'
    elif saw_budget_defer:
        state = 'PRELIMINARY_BUDGET'
    else:
        state = 'READY_FOR_COMMON_FIDELITY'

    core = {
        'schema_version': MULTIFIDELITY_SCHEMA_VERSION,
        'authority_version': MULTIFIDELITY_SCREENING_VERSION,
        'plan_id': plan.plan_id,
        'plan_semantic_sha256': plan.semantic_sha256,
        'stage_results': [
            item.model_dump(mode='json') for item in result_tuple
        ],
        'surviving_candidates': [
            item.model_dump(mode='json') for item in active
        ],
        'state': state,
        'created_at_utc': created_at_utc,
    }
    digest = _digest(core)
    return MultiFidelityScreeningEvaluation(
        evaluation_id=f'multifidelity-screening:{digest}',
        semantic_sha256=digest,
        plan_id=plan.plan_id,
        plan_semantic_sha256=plan.semantic_sha256,
        stage_results=result_tuple,
        surviving_candidates=active,
        state=state,
        created_at_utc=created_at_utc,
    )


def finalize_o100_multifidelity(
    *,
    plan: MultiFidelityPlan,
    screening: MultiFidelityScreeningEvaluation,
    final_comparison: TopologyComparisonEvaluation,
) -> MultiFidelityFinalization:
    if plan.domain != 'o100_topology':
        raise ValueError('O100 finalization requires o100_topology plan')
    if (
        screening.plan_id != plan.plan_id
        or screening.plan_semantic_sha256 != plan.semantic_sha256
    ):
        raise ValueError('screening evaluation belongs to another plan')

    final_stage = plan.stages[-1]
    if final_stage.policy != 'final_common_fidelity':
        raise ValueError('O100 final stage must require common fidelity')

    candidate_by_id = {
        item.authority_id: item
        for item in plan.candidates
        if item.authority_kind == 'system_variant'
    }
    if len(candidate_by_id) != len(plan.candidates):
        raise ValueError(
            'O100 multi-fidelity candidates must be system_variant authorities'
        )

    survivor_ids = {
        item.authority_id for item in screening.surviving_candidates
    }
    eligible_ids = {
        item.variant_id
        for item in final_comparison.eligibility
        if item.state == 'ELIGIBLE'
    }
    if survivor_ids != eligible_ids:
        raise ValueError(
            'final O100 comparison eligible set must equal exact screening survivors'
        )
    bundle_by_variant = {
        bundle.variant_id: bundle
        for bundle in final_comparison.bundles
    }
    for item in final_comparison.eligibility:
        candidate = candidate_by_id.get(item.variant_id)
        if candidate is None:
            raise ValueError(
                'final O100 comparison contains candidate outside multi-fidelity plan'
            )
        if item.state != 'ELIGIBLE':
            continue
        bundle_ref = bundle_by_variant.get(item.variant_id)
        if bundle_ref is None:
            raise ValueError(
                'final O100 eligible candidate requires exact VariantEvaluationBundle ref'
            )
        if candidate.semantic_sha256 != bundle_ref.variant_sha256:
            raise ValueError(
                'final O100 comparison SystemVariant hash differs from plan candidate'
            )

    if screening.state == 'READY_FOR_COMMON_FIDELITY':
        claim_state: FinalClaimState = 'COMPLETE'
    elif screening.state == 'PRELIMINARY_BUDGET':
        claim_state = 'PRELIMINARY_BUDGET'
    else:
        claim_state = 'BLOCKED_EVIDENCE'

    comparison_ref = MultiFidelityAuthorityRef(
        authority_kind='topology_comparison_evaluation',
        authority_id=final_comparison.evaluation_id,
        authority_version=final_comparison.authority_version,
        semantic_sha256=final_comparison.evaluation_sha256,
        evaluator_id='o100d-topology-comparison',
        evaluator_version=final_comparison.authority_version,
        fidelity='common-final-comparison',
    )
    final_candidates = tuple(
        item
        for item in plan.candidates
        if item.authority_id in survivor_ids
    )
    core = {
        'schema_version': MULTIFIDELITY_SCHEMA_VERSION,
        'authority_version': MULTIFIDELITY_FINALIZATION_VERSION,
        'screening_evaluation_id': screening.evaluation_id,
        'screening_evaluation_sha256': screening.semantic_sha256,
        'final_stage_id': final_stage.stage_id,
        'final_comparison_ref': comparison_ref.model_dump(mode='json'),
        'final_candidates': [
            item.model_dump(mode='json') for item in final_candidates
        ],
        'claim_state': claim_state,
    }
    digest = _digest(core)
    return MultiFidelityFinalization(
        finalization_id=f'multifidelity-finalization:{digest}',
        semantic_sha256=digest,
        screening_evaluation_id=screening.evaluation_id,
        screening_evaluation_sha256=screening.semantic_sha256,
        final_stage_id=final_stage.stage_id,
        final_comparison_ref=comparison_ref,
        final_candidates=final_candidates,
        claim_state=claim_state,
    )


class CadMultiFidelityRepository:
    """Append-only shared O90C/O100E audit persistence.

    This repository persists the generic stage plan/results. Domain-specific
    evaluation authorities remain in their own repositories.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        topology_comparison_repository: TopologyComparisonEvaluationResolver | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.topology_comparison_repository = topology_comparison_repository
        self.path = Path(scene_repository.path)
        if (
            topology_comparison_repository is not None
            and Path(topology_comparison_repository.path) != self.path
        ):
            raise ValueError(
                'multi-fidelity and topology comparison repositories must share '
                'one native CAD database'
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
                CREATE TABLE IF NOT EXISTS cad_multifidelity_plans (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    domain TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cad_multifidelity_stage_results (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    result_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    plan_id TEXT NOT NULL,
                    stage_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL,
                    FOREIGN KEY(plan_id)
                        REFERENCES cad_multifidelity_plans(plan_id)
                );
                CREATE INDEX IF NOT EXISTS idx_multifidelity_stage_plan_seq
                    ON cad_multifidelity_stage_results(plan_id, seq ASC);

                CREATE TABLE IF NOT EXISTS cad_multifidelity_screening_evaluations (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    evaluation_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    plan_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL,
                    FOREIGN KEY(plan_id)
                        REFERENCES cad_multifidelity_plans(plan_id)
                );

                CREATE TABLE IF NOT EXISTS cad_multifidelity_finalizations (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    finalization_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    screening_evaluation_id TEXT NOT NULL,
                    final_comparison_evaluation_id TEXT NOT NULL,
                    claim_state TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL,
                    FOREIGN KEY(screening_evaluation_id)
                        REFERENCES cad_multifidelity_screening_evaluations(evaluation_id)
                );
                CREATE INDEX IF NOT EXISTS idx_multifidelity_finalization_screening_seq
                    ON cad_multifidelity_finalizations(
                        screening_evaluation_id,
                        seq ASC
                    );
                """
            )

    def save_plan(self, plan: MultiFidelityPlan) -> MultiFidelityPlan:
        plan = MultiFidelityPlan.model_validate(plan.model_dump(mode='python'))
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT payload_json FROM cad_multifidelity_plans WHERE plan_id=?',
                (plan.plan_id,),
            ).fetchone()
            if existing is not None:
                persisted = MultiFidelityPlan.model_validate_json(
                    existing['payload_json']
                )
                if persisted != plan:
                    raise ValueError(
                        'MultiFidelityPlan id exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_multifidelity_plans(
                    plan_id, semantic_sha256, domain, payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.semantic_sha256,
                    plan.domain,
                    plan.model_dump_json(),
                    _utc_now(),
                ),
            )
        return plan

    def get_plan(self, plan_id: str) -> MultiFidelityPlan | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_multifidelity_plans WHERE plan_id=?',
                (plan_id,),
            ).fetchone()
        return (
            None
            if row is None
            else MultiFidelityPlan.model_validate_json(row['payload_json'])
        )

    def save_stage_result(
        self,
        result: MultiFidelityStageResult,
    ) -> MultiFidelityStageResult:
        result = MultiFidelityStageResult.model_validate(
            result.model_dump(mode='python')
        )
        plan = self.get_plan(result.plan_id)
        if plan is None:
            raise ValueError('stage result references unpersisted multi-fidelity plan')
        if result.plan_semantic_sha256 != plan.semantic_sha256:
            raise ValueError('stage result multi-fidelity plan hash mismatch')
        regenerated = build_multifidelity_stage_result(
            plan=plan,
            stage_id=result.stage_id,
            input_candidates=result.input_candidates,
            outcomes=result.outcomes,
        )
        if regenerated != result:
            raise ValueError('stage result does not reproduce from persisted plan')

        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_multifidelity_stage_results
                WHERE result_id=?
                """,
                (result.result_id,),
            ).fetchone()
            if existing is not None:
                persisted = MultiFidelityStageResult.model_validate_json(
                    existing['payload_json']
                )
                if persisted != result:
                    raise ValueError(
                        'MultiFidelityStageResult id exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_multifidelity_stage_results(
                    result_id, semantic_sha256, plan_id, stage_id,
                    payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    result.result_id,
                    result.semantic_sha256,
                    result.plan_id,
                    result.stage_id,
                    result.model_dump_json(),
                    _utc_now(),
                ),
            )
        return result

    def get_stage_result(
        self,
        result_id: str,
    ) -> MultiFidelityStageResult | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_multifidelity_stage_results
                WHERE result_id=?
                """,
                (result_id,),
            ).fetchone()
        if row is None:
            return None
        result = MultiFidelityStageResult.model_validate_json(row['payload_json'])
        plan = self.get_plan(result.plan_id)
        if plan is None:
            raise ValueError('persisted stage result plan disappeared')
        regenerated = build_multifidelity_stage_result(
            plan=plan,
            stage_id=result.stage_id,
            input_candidates=result.input_candidates,
            outcomes=result.outcomes,
        )
        if regenerated != result:
            raise ValueError('persisted stage result no longer reproduces')
        return result

    def save_screening(
        self,
        evaluation: MultiFidelityScreeningEvaluation,
    ) -> MultiFidelityScreeningEvaluation:
        evaluation = MultiFidelityScreeningEvaluation.model_validate(
            evaluation.model_dump(mode='python')
        )
        plan = self.get_plan(evaluation.plan_id)
        if plan is None:
            raise ValueError('screening references unpersisted multi-fidelity plan')
        persisted_results: list[MultiFidelityStageResult] = []
        for item in evaluation.stage_results:
            stored = self.get_stage_result(item.result_id)
            if stored is None or stored != item:
                raise ValueError(
                    'screening references missing or mismatched stage result'
                )
            persisted_results.append(stored)
        regenerated = evaluate_multifidelity_screening(
            plan=plan,
            stage_results=persisted_results,
            created_at_utc=evaluation.created_at_utc,
        )
        if regenerated != evaluation:
            raise ValueError('screening does not reproduce from persisted authorities')

        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_multifidelity_screening_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation.evaluation_id,),
            ).fetchone()
            if existing is not None:
                persisted = MultiFidelityScreeningEvaluation.model_validate_json(
                    existing['payload_json']
                )
                if persisted != evaluation:
                    raise ValueError(
                        'screening evaluation id exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_multifidelity_screening_evaluations(
                    evaluation_id, semantic_sha256, plan_id, state,
                    payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.semantic_sha256,
                    evaluation.plan_id,
                    evaluation.state,
                    evaluation.model_dump_json(),
                    _utc_now(),
                ),
            )
        return evaluation

    def get_screening(
        self,
        evaluation_id: str,
    ) -> MultiFidelityScreeningEvaluation | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_multifidelity_screening_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        evaluation = MultiFidelityScreeningEvaluation.model_validate_json(
            row['payload_json']
        )
        plan = self.get_plan(evaluation.plan_id)
        if plan is None:
            raise ValueError('persisted screening plan disappeared')
        results = tuple(
            self.get_stage_result(item.result_id)
            for item in evaluation.stage_results
        )
        if any(item is None for item in results):
            raise ValueError('persisted screening stage result disappeared')
        regenerated = evaluate_multifidelity_screening(
            plan=plan,
            stage_results=tuple(
                item for item in results if item is not None
            ),
            created_at_utc=evaluation.created_at_utc,
        )
        if regenerated != evaluation:
            raise ValueError('persisted screening no longer reproduces')
        return evaluation


    def _validate_finalization(
        self,
        finalization: MultiFidelityFinalization,
    ) -> MultiFidelityFinalization:
        finalization = MultiFidelityFinalization.model_validate(
            finalization.model_dump(mode='python')
        )
        screening = self.get_screening(
            finalization.screening_evaluation_id
        )
        if screening is None:
            raise ValueError(
                'multi-fidelity finalization references missing screening evaluation'
            )
        if screening.semantic_sha256 != finalization.screening_evaluation_sha256:
            raise ValueError(
                'multi-fidelity finalization screening hash mismatch'
            )
        plan = self.get_plan(screening.plan_id)
        if plan is None:
            raise ValueError('multi-fidelity finalization plan disappeared')
        if plan.domain != 'o100_topology':
            raise ValueError(
                'persisted final comparison adapter currently supports '
                'o100_topology only'
            )
        if self.topology_comparison_repository is None:
            raise ValueError(
                'O100 multi-fidelity finalization requires a typed '
                'topology comparison repository'
            )
        final_comparison = self.topology_comparison_repository.get_evaluation(
            finalization.final_comparison_ref.authority_id
        )
        if final_comparison is None:
            raise ValueError(
                'multi-fidelity finalization references missing final comparison'
            )
        if (
            final_comparison.evaluation_sha256
            != finalization.final_comparison_ref.semantic_sha256
        ):
            raise ValueError(
                'multi-fidelity final comparison exact hash mismatch'
            )
        regenerated = finalize_o100_multifidelity(
            plan=plan,
            screening=screening,
            final_comparison=final_comparison,
        )
        if regenerated != finalization:
            raise ValueError(
                'multi-fidelity finalization does not reproduce from '
                'persisted exact authorities'
            )
        return finalization

    def save_finalization(
        self,
        finalization: MultiFidelityFinalization,
    ) -> MultiFidelityFinalization:
        finalization = self._validate_finalization(finalization)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_multifidelity_finalizations
                WHERE finalization_id=?
                """,
                (finalization.finalization_id,),
            ).fetchone()
            if existing is not None:
                persisted = MultiFidelityFinalization.model_validate_json(
                    existing['payload_json']
                )
                if persisted != finalization:
                    raise ValueError(
                        'MultiFidelityFinalization id exists with different semantics'
                    )
                return self._validate_finalization(persisted)
            connection.execute(
                """
                INSERT INTO cad_multifidelity_finalizations(
                    finalization_id,
                    semantic_sha256,
                    screening_evaluation_id,
                    final_comparison_evaluation_id,
                    claim_state,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    finalization.finalization_id,
                    finalization.semantic_sha256,
                    finalization.screening_evaluation_id,
                    finalization.final_comparison_ref.authority_id,
                    finalization.claim_state,
                    finalization.model_dump_json(),
                    _utc_now(),
                ),
            )
        return finalization

    def get_finalization(
        self,
        finalization_id: str,
    ) -> MultiFidelityFinalization | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_multifidelity_finalizations
                WHERE finalization_id=?
                """,
                (finalization_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate_finalization(
            MultiFidelityFinalization.model_validate_json(
                row['payload_json']
            )
        )
