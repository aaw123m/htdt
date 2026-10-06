"""Optimizer-algorithm qualification authority (#675, REV58-VALIDMETH).

An acoustically valid objective function and a physically robust candidate
do **not** prove that the optimizer reliably found a good or globally
competitive solution. Robustness (#4/#604) answers "does this design
survive uncertainty?"; this authority answers a different question: given
the exact same optimization problem, how trustworthy is the search
procedure's claim that these are the best candidates it could find under
the declared budget?

This module is the fail-closed layer that separates *algorithm* evidence
from *physical* evidence:

- :class:`OptimizationProblemIdentity` — the sealed declaration of the
  exact problem: decision variables + bounds, objectives + units +
  directions, hard constraints, solver/prediction fidelity, seat/source
  weighting, evaluation-cache version. Changing any of them defines a
  different problem; runs under different identities never share one
  leaderboard row (#675 §1).

- :class:`OptimizerAlgorithmSpec` — the sealed algorithm identity:
  family, implementation + version, parameters, restart/population
  settings, RNG + seed policy, parallelism, surrogate state and the
  termination rule. ``NSGA-II`` alone is not an identity (#675 §2).

- :class:`OptimizerRunProfile` — the sealed benchmark profile binding one
  problem identity + one algorithm identity + the per-seed run set +
  baseline declarations + known-optimum fixture pins. ``budget`` is a
  first-class record — evaluation counts per fidelity, not wall time
  alone (#675 §3).

- :class:`OptimizationRunQualification` +
  :func:`evaluate_optimizer_qualification` — the sealed verdict.
  ``global_optimum`` is reserved for problems where optimality is
  mathematically or exhaustively established; everything else reports
  ``best_found_under_budget`` (#675 §18). A stochastic algorithm with a
  single run can never claim more than ``limited`` evidence.

- :class:`ParetoApproximationAssessment` +
  :func:`evaluate_pareto_approximation` — multi-objective quality with
  convergence, diversity/coverage and feasibility reported as separate
  observables, never one opaque ``pareto_quality`` scalar (#675 §12).
  The reference-front status is explicit
  (``exact``/``exhaustive``/``best_known_aggregate``/``none``): a union
  of optimizer outputs is never labeled the true front (#675 §13).

Composition (no parallel truth stores):

- #577 ``cad_decision_rule`` — near-tie/materiality decisions stay with
  the decision-rule authority; this module supplies search-reliability
  evidence, not winner selection.
- #604/O90 ``optimization_robustness*`` — physical robustness of a
  candidate is a separate axis; a physically robust candidate found in
  1 of 30 seeds is an *algorithm* reliability issue (#675 §14).
- #566 ``acoustic_validation_envelope`` — solver accuracy envelopes stay
  solver-side; optimizer fixtures pin their solver/fidelity refs.
- #689 ``cad_parameter_identifiability`` — a parameter that cannot be
  identified cannot be "optimized away"; this authority never re-derives
  identifiability.

Literature basis
----------------
- Liu et al. (2017), "Benchmarking Stochastic Algorithms for Global
  Optimization Problems by Visualizing Confidence Intervals", IEEE Trans.
  Cybernetics 47(9) — DOI 10.1109/TCYB.2017.2659659. Basis for the
  multi-seed variability requirement: one successful seed or one mean
  objective is not reliability evidence.
- Hendrix & Lančinskas (2015), "On Benchmarking Stochastic Global
  Optimization Algorithms", Informatica 26(2) — DOI
  10.15388/Informatica.2015.69. Basis for finite-budget benchmarking and
  baseline comparison discipline.
- Multi-objective performance-assessment reviews (e.g. Applied Sciences
  11(7):3117, 2021) — convergence, diversity/coverage and feasibility are
  complementary indicators; hypervolume or GD alone is never a universal
  Pareto-quality scalar.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


OPTIMIZER_QUALIFICATION_SCHEMA_VERSION = 'optimizer-qualification-1'
OPTIMIZER_QUALIFICATION_EVALUATION_VERSION = 'optimizer-qual-eval-1'
PARETO_ASSESSMENT_VERSION = 'pareto-approximation-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _require_finite(value: float, label: str) -> None:
    if not isfinite(float(value)):
        raise ValueError(f'{label} must be finite')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{
            sha_field: digest,
            id_field: _semantic_id(prefix, digest),
        },
    )


def _require_ref_sha(ref: AuthorityRef | None, label: str) -> None:
    if ref is not None and ref.ref_sha256 is None:
        raise ValueError(f'{label} must pin its sha256')


# ----------------------------------------------------------------------
# Taxonomies

ObjectiveDirection = Literal['minimize', 'maximize', 'target']
"""How each objective is optimized (#675 §1) — direction is part of the
problem identity; flipping it silently defines a different problem."""

VariableKind = Literal['continuous', 'discrete', 'categorical', 'mixed']

FidelityLevel = Literal[
    'high_fidelity', 'low_fidelity', 'surrogate', 'mixed', 'unknown'
]
"""The fidelity axis a budget is counted on (#675 §3/§10) — a surrogate
evaluation is never silently counted as a solver evaluation."""

AlgorithmFamily = Literal[
    'random_search',
    'uniform_grid_enumeration',
    'multistart_local',
    'greedy_heuristic',
    'evolutionary_multiobjective',
    'bayesian_optimization',
    'cma_es',
    'simulated_annealing',
    'exhaustive_enumeration',
    'production_search',
    'custom_declared',
]
"""Stochastic and deterministic families are kept apart — an exhaustive
enumeration may claim exact optimality while an evolutionary run may not
(#675 §5/§6)."""

ConvergenceState = Literal[
    'still_improving_at_budget',
    'plateau_within_tested_budget',
    'converged_by_declared_criterion',
    'no_convergence_evidence',
]
"""A plateau is empirical evidence, never mathematical global convergence
(#675 §7)."""

ParetoReferenceStatus = Literal[
    'exact_reference_front',
    'exhaustive_discrete_front',
    'best_known_aggregate_front',
    'no_reference_front',
]
"""#675 §13 — with no reference front the authority reports empirical
attainment/coverage behavior and limitations, never a front-quality
scalar."""

OptimalityClaim = Literal[
    'best_found_under_budget',
    'global_optimum_proven',
]
"""Product language (#675 §18): ``global_optimum_proven`` is reserved for
mathematically/exhaustively established cases only."""

QualificationState = Literal[
    'qualified',
    'qualified_with_limitations',
    'unqualified',
    'insufficient_evidence',
]
"""The fail-closed verdict on the search qualification."""

ParetoAssessmentState = Literal[
    'assessed_with_reference',
    'assessed_empirical_only',
    'assessed_with_limitations',
    'insufficient_evidence',
]

OptimizerFixtureId = Literal[
    'OPT10', 'OPT20', 'OPT30', 'OPT40', 'OPT50', 'OPT60', 'OPT70',
    'OPT80', 'OPT90',
]
"""#675 fixtures: OPT10 1-D known optimum, OPT20 discrete placement
enumeration vs exhaustive optimum, OPT30 deceptive local minima,
OPT40 narrow feasible region, OPT50 seed sensitivity, OPT60 budget
curve, OPT70 exact two-objective Pareto set, OPT80 low-fidelity
screening miss, OPT90 physical-robust/search-unstable."""


# ----------------------------------------------------------------------
# Problem & algorithm identity

class DecisionVariableSpec(BaseModel):
    """One decision variable of the optimization problem (#675 §1)."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    kind: VariableKind
    bounds: str | None = None
    """Declared bounds/domain description — exact ranges live inside the
    pinned SearchSpec/scene refs; this field documents intent."""
    unit: str | None = None


class ObjectiveDefinition(BaseModel):
    """One objective's identity — definition, unit and direction are all
    part of the problem (#675 §1)."""

    model_config = ConfigDict(frozen=True)

    objective_id: str = Field(min_length=1)
    definition_ref: AuthorityRef | None = None
    """Pins the owning objective authority record when one exists."""
    unit: str | None = None
    direction: ObjectiveDirection
    weighting: str | None = None
    """Seat/source weighting declaration; a change defines a different
    problem."""

    @model_validator(mode='after')
    def _check(self) -> 'ObjectiveDefinition':
        _require_ref_sha(self.definition_ref, 'objective definition_ref')
        return self


class OptimizationProblemIdentity(BaseModel):
    """The sealed exact optimization problem (#675 §1).

    Runs whose objective semantics differ are never compared on one
    leaderboard row — the problem identity is content-addressed so a
    changed objective/constraint/fidelity yields a different identity.
    """

    model_config = ConfigDict(frozen=True)

    problem_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    problem_label: str = Field(min_length=1)
    scene_revision_ref: AuthorityRef | None = None
    system_variant_ref: AuthorityRef | None = None
    search_spec_ref: AuthorityRef | None = None
    decision_variables: tuple[DecisionVariableSpec, ...] = Field(
        min_length=1
    )
    objectives: tuple[ObjectiveDefinition, ...] = Field(min_length=1)
    hard_constraints: tuple[str, ...] = ()
    solver_fidelity: FidelityLevel = 'unknown'
    solver_ref: AuthorityRef | None = None
    prediction_model_ref: AuthorityRef | None = None
    robustness_spec_ref: AuthorityRef | None = None
    """#4/#604 robustness spec pin when the problem is robust-aware."""
    evaluation_cache_version: str | None = None
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=OPTIMIZER_QUALIFICATION_SCHEMA_VERSION, min_length=1
    )
    problem_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'problem_label': self.problem_label,
            'scene_revision_ref': (
                self.scene_revision_ref.model_dump(mode='json')
                if self.scene_revision_ref is not None
                else None
            ),
            'system_variant_ref': (
                self.system_variant_ref.model_dump(mode='json')
                if self.system_variant_ref is not None
                else None
            ),
            'search_spec_ref': (
                self.search_spec_ref.model_dump(mode='json')
                if self.search_spec_ref is not None
                else None
            ),
            'decision_variables': [
                v.model_dump(mode='json') for v in self.decision_variables
            ],
            'objectives': [
                o.model_dump(mode='json') for o in self.objectives
            ],
            'hard_constraints': list(self.hard_constraints),
            'solver_fidelity': self.solver_fidelity,
            'solver_ref': (
                self.solver_ref.model_dump(mode='json')
                if self.solver_ref is not None
                else None
            ),
            'prediction_model_ref': (
                self.prediction_model_ref.model_dump(mode='json')
                if self.prediction_model_ref is not None
                else None
            ),
            'robustness_spec_ref': (
                self.robustness_spec_ref.model_dump(mode='json')
                if self.robustness_spec_ref is not None
                else None
            ),
            'evaluation_cache_version': self.evaluation_cache_version,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'OptimizationProblemIdentity':
        _require_iso8601(self.declared_at_utc, 'problem declared_at_utc')
        for ref, label in (
            (self.scene_revision_ref, 'scene_revision_ref'),
            (self.system_variant_ref, 'system_variant_ref'),
            (self.search_spec_ref, 'search_spec_ref'),
            (self.solver_ref, 'solver_ref'),
            (self.prediction_model_ref, 'prediction_model_ref'),
            (self.robustness_spec_ref, 'robustness_spec_ref'),
        ):
            _require_ref_sha(ref, label)
        names = [v.name for v in self.decision_variables]
        if len(set(names)) != len(names):
            raise ValueError('decision variable names must be unique')
        ids = [o.objective_id for o in self.objectives]
        if len(set(ids)) != len(ids):
            raise ValueError('objective ids must be unique')
        expected = _hash(self.identity_payload())
        if self.problem_sha256 != expected:
            raise ValueError('optimization problem hash mismatch')
        if self.problem_id != _semantic_id('optprob', expected):
            raise ValueError(
                'optimization problem id does not match its hash'
            )
        return self


def build_optimization_problem(
    *,
    document_id: str,
    problem_label: str,
    decision_variables: Sequence[DecisionVariableSpec],
    objectives: Sequence[ObjectiveDefinition],
    scene_revision_ref: AuthorityRef | None = None,
    system_variant_ref: AuthorityRef | None = None,
    search_spec_ref: AuthorityRef | None = None,
    hard_constraints: Sequence[str] = (),
    solver_fidelity: FidelityLevel = 'unknown',
    solver_ref: AuthorityRef | None = None,
    prediction_model_ref: AuthorityRef | None = None,
    robustness_spec_ref: AuthorityRef | None = None,
    evaluation_cache_version: str | None = None,
    declared_at_utc: str | None = None,
) -> OptimizationProblemIdentity:
    """Seal one exact optimization-problem identity."""
    return _seal(
        OptimizationProblemIdentity,
        {
            'document_id': document_id,
            'problem_label': problem_label,
            'decision_variables': [
                v.model_dump(mode='json') for v in decision_variables
            ],
            'objectives': [
                o.model_dump(mode='json') for o in objectives
            ],
            'scene_revision_ref': (
                scene_revision_ref.model_dump(mode='json')
                if scene_revision_ref is not None
                else None
            ),
            'system_variant_ref': (
                system_variant_ref.model_dump(mode='json')
                if system_variant_ref is not None
                else None
            ),
            'search_spec_ref': (
                search_spec_ref.model_dump(mode='json')
                if search_spec_ref is not None
                else None
            ),
            'hard_constraints': list(hard_constraints),
            'solver_fidelity': solver_fidelity,
            'solver_ref': (
                solver_ref.model_dump(mode='json')
                if solver_ref is not None
                else None
            ),
            'prediction_model_ref': (
                prediction_model_ref.model_dump(mode='json')
                if prediction_model_ref is not None
                else None
            ),
            'robustness_spec_ref': (
                robustness_spec_ref.model_dump(mode='json')
                if robustness_spec_ref is not None
                else None
            ),
            'evaluation_cache_version': evaluation_cache_version,
            'declared_at_utc': declared_at_utc or _utc_now(),
        },
        'problem_id',
        'problem_sha256',
        'optprob',
    )


def problem_binding(
    problem: OptimizationProblemIdentity,
) -> AuthorityRef:
    return AuthorityRef(
        kind='optimization_problem_identity',
        ref_id=problem.problem_id,
        ref_sha256=problem.problem_sha256,
    )


class SearchBudget(BaseModel):
    """The declared evaluation budget (#675 §3).

    Function evaluations — split by fidelity — are the primary budget for
    expensive black-box acoustics; wall-clock is context, never a
    substitute.
    """

    model_config = ConfigDict(frozen=True)

    function_evaluations: int | None = Field(default=None, ge=0)
    high_fidelity_evaluations: int | None = Field(default=None, ge=0)
    low_fidelity_evaluations: int | None = Field(default=None, ge=0)
    surrogate_evaluations: int | None = Field(default=None, ge=0)
    iterations_or_generations: int | None = Field(default=None, ge=0)
    restart_count: int | None = Field(default=None, ge=0)
    wall_clock_s: float | None = Field(default=None, ge=0.0)
    environment_label: str | None = None
    """Hardware/software context — required when wall-clock comparisons
    are claimed (#675 §17)."""

    @model_validator(mode='after')
    def _check(self) -> 'SearchBudget':
        if self.wall_clock_s is not None:
            _require_finite(self.wall_clock_s, 'budget wall_clock_s')
        return self


class SurrogateSpec(BaseModel):
    """Model-assisted search declaration (#675 §11).

    A surrogate-predicted optimum is not the acoustic-solver optimum —
    the profile keeps surrogate error evidence separate and the
    qualification requires common-fidelity final evaluation.
    """

    model_config = ConfigDict(frozen=True)

    model_family: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    training_sample_count: int | None = Field(default=None, ge=0)
    acquisition_function: str | None = None
    cross_validation_error: float | None = None
    holdout_error: float | None = None
    retraining_schedule: str | None = None
    extrapolation_warnings: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'SurrogateSpec':
        for label in ('cross_validation_error', 'holdout_error'):
            value = getattr(self, label)
            if value is not None:
                _require_finite(value, f'surrogate {label}')
        return self


class OptimizerAlgorithmSpec(BaseModel):
    """The sealed algorithm identity (#675 §2).

    ``NSGA-II``/``CMA-ES`` family names without parameters, version and
    RNG/seed policy are not identities. ``stochastic=True`` marks
    families whose results are seed-dependent.
    """

    model_config = ConfigDict(frozen=True)

    family: AlgorithmFamily
    implementation: str = Field(min_length=1)
    implementation_version: str = Field(min_length=1)
    parameters: tuple[str, ...] = ()
    """Canonical ``name=value`` parameter strings."""
    stochastic: bool = True
    rng_name: str | None = None
    rng_version: str | None = None
    seed_policy: str | None = None
    population_or_restart_settings: str | None = None
    parallelism_policy: str | None = None
    termination_rule: str = Field(min_length=1)
    surrogate: SurrogateSpec | None = None

    @model_validator(mode='after')
    def _check(self) -> 'OptimizerAlgorithmSpec':
        if len(set(self.parameters)) != len(self.parameters):
            raise ValueError('algorithm parameters must be unique')
        if self.stochastic and self.seed_policy is None:
            raise ValueError(
                'a stochastic algorithm must declare its seed policy'
            )
        return self


class IndependentRunRecord(BaseModel):
    """One independent optimizer run (#675 §4).

    Per-run evidence — seed, budget, final candidates and the best-so-far
    trajectory digest — is retained; summaries publish medians/spreads,
    never only the best seed.
    """

    model_config = ConfigDict(frozen=True)

    run_label: str = Field(min_length=1)
    seed: int | None = None
    budget: SearchBudget
    best_objective_value: float | None = None
    final_candidate_refs: tuple[AuthorityRef, ...] = ()
    final_front_ref: AuthorityRef | None = None
    feasible_count: int | None = Field(default=None, ge=0)
    infeasible_count: int | None = Field(default=None, ge=0)
    best_so_far_digest: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    """Content digest of the best-so-far trajectory artifact."""
    convergence_state: ConvergenceState = 'no_convergence_evidence'
    common_fidelity_reevaluated: bool = False
    """Whether final candidates were re-evaluated at the declared common
    fidelity (#675 §10)."""

    @model_validator(mode='after')
    def _check(self) -> 'IndependentRunRecord':
        if self.best_objective_value is not None:
            _require_finite(
                self.best_objective_value, 'run best_objective_value'
            )
        for ref in self.final_candidate_refs:
            _require_ref_sha(ref, 'run final_candidate_refs')
        _require_ref_sha(self.final_front_ref, 'run final_front_ref')
        if (
            self.feasible_count is not None
            and self.infeasible_count is not None
            and self.budget.function_evaluations is not None
            and (
                self.feasible_count + self.infeasible_count
                > self.budget.function_evaluations
            )
        ):
            raise ValueError(
                'feasible+infeasible counts cannot exceed the declared '
                'evaluation budget'
            )
        return self


class BaselineDeclaration(BaseModel):
    """One baseline algorithm the production search is compared against
    under equal budget (#675 §6)."""

    model_config = ConfigDict(frozen=True)

    baseline_family: AlgorithmFamily
    algorithm_ref: AuthorityRef | None = None
    equal_budget: bool = False
    outcome_summary: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'BaselineDeclaration':
        _require_ref_sha(self.algorithm_ref, 'baseline algorithm_ref')
        if not self.equal_budget:
            raise ValueError(
                'a baseline comparison without equal budget is not '
                'admissible evidence'
            )
        return self


class KnownOptimumFixturePin(BaseModel):
    """A benchmark fixture whose optimum is independently known or can be
    exhaustively enumerated (#675 §5)."""

    model_config = ConfigDict(frozen=True)

    fixture_id: str = Field(min_length=1)
    """OPT10..OPT90 or a retained-suite fixture id."""
    fixture_ref: AuthorityRef | None = None
    known_optimum_basis: Literal[
        'analytic', 'exhaustive_enumeration', 'reference_solution',
        'symmetric_equivalence',
    ]
    recovered: bool | None = None
    """Whether the algorithm recovered the known optimum — ``None`` when
    the fixture was pinned but not run."""
    regret: float | None = Field(default=None, ge=0.0)
    """Gap between best-found and the known optimum where numeric."""

    @model_validator(mode='after')
    def _check(self) -> 'KnownOptimumFixturePin':
        _require_ref_sha(self.fixture_ref, 'fixture fixture_ref')
        if self.regret is not None:
            _require_finite(self.regret, 'fixture regret')
        return self


class MultiFidelityPolicy(BaseModel):
    """The low→high fidelity promotion contract (#675 §10)."""

    model_config = ConfigDict(frozen=True)

    screening_fidelity: FidelityLevel | None = None
    final_fidelity: FidelityLevel | None = None
    finalist_promotion_rule: str | None = None
    rank_correlation_between_fidelities: float | None = None
    common_fidelity_final_evaluation: bool = False

    @model_validator(mode='after')
    def _check(self) -> 'MultiFidelityPolicy':
        if self.rank_correlation_between_fidelities is not None:
            value = self.rank_correlation_between_fidelities
            _require_finite(value, 'rank_correlation_between_fidelities')
            if not (-1.0 <= value <= 1.0):
                raise ValueError(
                    'rank correlation must lie in [-1, 1]'
                )
        return self


class OptimizerRunProfile(BaseModel):
    """The sealed benchmark profile for one optimization problem + one
    algorithm (#675 §1–§11).

    Binds the problem identity, algorithm identity, the independent-run
    set, baselines, known-optimum fixtures and the multi-fidelity policy.
    Any change is a new profile, not an edit.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    problem_ref: AuthorityRef
    algorithm: OptimizerAlgorithmSpec
    declared_budget: SearchBudget
    runs: tuple[IndependentRunRecord, ...] = Field(min_length=1)
    baselines: tuple[BaselineDeclaration, ...] = ()
    known_optimum_fixtures: tuple[KnownOptimumFixturePin, ...] = ()
    multi_fidelity: MultiFidelityPolicy | None = None
    multi_objective: bool = False
    optimality_claim_requested: OptimalityClaim = (
        'best_found_under_budget'
    )
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=OPTIMIZER_QUALIFICATION_SCHEMA_VERSION, min_length=1
    )
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'problem_ref': self.problem_ref.model_dump(mode='json'),
            'algorithm': self.algorithm.model_dump(mode='json'),
            'declared_budget': self.declared_budget.model_dump(
                mode='json'
            ),
            'runs': [r.model_dump(mode='json') for r in self.runs],
            'baselines': [
                b.model_dump(mode='json') for b in self.baselines
            ],
            'known_optimum_fixtures': [
                f.model_dump(mode='json')
                for f in self.known_optimum_fixtures
            ],
            'multi_fidelity': (
                self.multi_fidelity.model_dump(mode='json')
                if self.multi_fidelity is not None
                else None
            ),
            'multi_objective': self.multi_objective,
            'optimality_claim_requested': self.optimality_claim_requested,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'OptimizerRunProfile':
        _require_iso8601(self.declared_at_utc, 'profile declared_at_utc')
        if self.problem_ref.kind != 'optimization_problem_identity':
            raise ValueError(
                "problem_ref must pin an "
                "'optimization_problem_identity'"
            )
        _require_ref_sha(self.problem_ref, 'problem_ref')
        labels = [r.run_label for r in self.runs]
        if len(set(labels)) != len(labels):
            raise ValueError('run labels must be unique')
        if self.algorithm.stochastic:
            seeds = [
                r.seed for r in self.runs if r.seed is not None
            ]
            if len(seeds) != len(set(seeds)):
                raise ValueError(
                    'stochastic runs must not repeat seeds'
                )
        if self.multi_objective is False and any(
            r.final_front_ref is not None for r in self.runs
        ):
            raise ValueError(
                'single-objective profile cannot carry front refs'
            )
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('optimizer run profile hash mismatch')
        if self.profile_id != _semantic_id('optprof', expected):
            raise ValueError(
                'optimizer run profile id does not match its hash'
            )
        return self

    @property
    def run_count(self) -> int:
        return len(self.runs)


def build_optimizer_run_profile(
    *,
    document_id: str,
    problem_ref: AuthorityRef,
    algorithm: OptimizerAlgorithmSpec,
    declared_budget: SearchBudget,
    runs: Sequence[IndependentRunRecord],
    baselines: Sequence[BaselineDeclaration] = (),
    known_optimum_fixtures: Sequence[KnownOptimumFixturePin] = (),
    multi_fidelity: MultiFidelityPolicy | None = None,
    multi_objective: bool = False,
    optimality_claim_requested: OptimalityClaim = (
        'best_found_under_budget'
    ),
    declared_at_utc: str | None = None,
) -> OptimizerRunProfile:
    """Seal one optimizer benchmark profile."""
    return _seal(
        OptimizerRunProfile,
        {
            'document_id': document_id,
            'problem_ref': problem_ref.model_dump(mode='json'),
            'algorithm': algorithm.model_dump(mode='json'),
            'declared_budget': declared_budget.model_dump(mode='json'),
            'runs': [r.model_dump(mode='json') for r in runs],
            'baselines': [b.model_dump(mode='json') for b in baselines],
            'known_optimum_fixtures': [
                f.model_dump(mode='json') for f in known_optimum_fixtures
            ],
            'multi_fidelity': (
                multi_fidelity.model_dump(mode='json')
                if multi_fidelity is not None
                else None
            ),
            'multi_objective': multi_objective,
            'optimality_claim_requested': optimality_claim_requested,
            'declared_at_utc': declared_at_utc or _utc_now(),
        },
        'profile_id',
        'profile_sha256',
        'optprof',
    )


def run_profile_binding(
    profile: OptimizerRunProfile,
) -> AuthorityRef:
    return AuthorityRef(
        kind='optimizer_run_profile',
        ref_id=profile.profile_id,
        ref_sha256=profile.profile_sha256,
    )


# ----------------------------------------------------------------------
# Qualification verdict

class OptimizationRunQualification(BaseModel):
    """The sealed fail-closed verdict on an optimizer profile (#675)."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    state: QualificationState
    optimality_claim: OptimalityClaim
    """The strongest claim the evidence supports — never stronger than
    the profile's request."""
    convergence_state: ConvergenceState
    run_count: int = Field(ge=1)
    seed_variability_observed: bool | None = None
    baselines_compared: int = Field(ge=0)
    known_optimum_recovered: int = Field(ge=0)
    common_fidelity_finalized: bool
    regression_flags: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(
        default=OPTIMIZER_QUALIFICATION_EVALUATION_VERSION, min_length=1
    )
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'state': self.state,
            'optimality_claim': self.optimality_claim,
            'convergence_state': self.convergence_state,
            'run_count': self.run_count,
            'seed_variability_observed': self.seed_variability_observed,
            'baselines_compared': self.baselines_compared,
            'known_optimum_recovered': self.known_optimum_recovered,
            'common_fidelity_finalized': self.common_fidelity_finalized,
            'regression_flags': list(self.regression_flags),
            'reasons': list(self.reasons),
            'limitations': list(self.limitations),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'OptimizationRunQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        if self.profile_ref.kind != 'optimizer_run_profile':
            raise ValueError(
                "profile_ref must pin an 'optimizer_run_profile'"
            )
        _require_ref_sha(self.profile_ref, 'profile_ref')
        if self.state == 'qualified' and self.limitations:
            raise ValueError(
                'a fully qualified verdict cannot carry limitations'
            )
        if self.optimality_claim == 'global_optimum_proven' and (
            self.state != 'qualified'
        ):
            raise ValueError(
                'global optimality requires a fully qualified verdict'
            )
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError(
                'optimizer qualification hash mismatch'
            )
        if self.qualification_id != _semantic_id('optqual', expected):
            raise ValueError(
                'optimizer qualification id does not match its hash'
            )
        return self


def optimizer_qualification_binding(
    qualification: OptimizationRunQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='optimization_run_qualification',
        ref_id=qualification.qualification_id,
        ref_sha256=qualification.qualification_sha256,
    )


def evaluate_optimizer_qualification(
    document_id: str,
    profile: OptimizerRunProfile,
    *,
    evaluated_at_utc: str | None = None,
) -> OptimizationRunQualification:
    """Fail-closed verdict on an optimizer benchmark profile (#675).

    Rules, in order:

    - No declared evaluation budget → ``insufficient_evidence``.
    - A stochastic algorithm evaluated from a single run caps at
      ``qualified_with_limitations`` — seed variability was never
      measured (#675 §4).
    - ``global_optimum_proven`` requires an
      ``exhaustive_enumeration``/exact family *and* at least one
      recovered known-optimum/exhaustive fixture; otherwise the claim
      collapses to ``best_found_under_budget`` with a reason.
    - Multi-fidelity profiles whose finalists were never re-evaluated at
      the declared common fidelity cap at ``qualified_with_limitations``
      (#675 §10).
    - Runs reporting ``no_convergence_evidence`` across the board cap at
      ``qualified_with_limitations``.
    - No baselines and no known-optimum fixtures →
      ``qualified_with_limitations`` at best: an unbenchmarked "optimal"
      is refused (#675 §5/§6).
    - Any run flagged infeasible-only (``feasible_count == 0``) is a
      limitation, never silent.
    """

    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')

    reasons: list[str] = []
    limitations: list[str] = []
    regression_flags: list[str] = []

    budget = profile.declared_budget
    total_evals = budget.function_evaluations
    if total_evals is None and (
        budget.high_fidelity_evaluations is None
        and budget.low_fidelity_evaluations is None
    ):
        state: QualificationState = 'insufficient_evidence'
        reasons.append(
            'no evaluation budget was declared — budget is first-class'
        )
    else:
        state = 'qualified'

    run_count = len(profile.runs)
    stochastic = profile.algorithm.stochastic
    seed_variability_observed: bool | None = None
    if stochastic:
        distinct_objectives = {
            r.best_objective_value
            for r in profile.runs
            if r.best_objective_value is not None
        }
        if run_count >= 2 and len(distinct_objectives) > 1:
            seed_variability_observed = True
        elif run_count >= 2:
            seed_variability_observed = False
        if run_count < 2:
            limitations.append(
                'stochastic algorithm qualified from a single run — '
                'seed variability was never measured'
            )
            if state == 'qualified':
                state = 'qualified_with_limitations'

    convergence_states = {r.convergence_state for r in profile.runs}
    if convergence_states == {'converged_by_declared_criterion'}:
        convergence_state: ConvergenceState = (
            'converged_by_declared_criterion'
        )
    elif 'still_improving_at_budget' in convergence_states:
        convergence_state = 'still_improving_at_budget'
        limitations.append(
            'at least one run was still improving at the declared '
            'budget'
        )
        if state == 'qualified':
            state = 'qualified_with_limitations'
    elif 'plateau_within_tested_budget' in convergence_states:
        convergence_state = 'plateau_within_tested_budget'
    else:
        convergence_state = 'no_convergence_evidence'
        limitations.append(
            'no run produced convergence evidence — an empirical '
            'plateau is not mathematical convergence'
        )
        if state == 'qualified':
            state = 'qualified_with_limitations'

    recovered = sum(
        1 for f in profile.known_optimum_fixtures if f.recovered
    )
    for fixture in profile.known_optimum_fixtures:
        if fixture.recovered is False:
            regression_flags.append(
                f'known-optimum fixture {fixture.fixture_id} not '
                'recovered'
            )
            state = 'unqualified' if state != 'insufficient_evidence' \
                else state
            reasons.append(
                f'fixture {fixture.fixture_id}: known optimum missed'
            )

    exact_family = profile.algorithm.family in (
        'exhaustive_enumeration',
        'uniform_grid_enumeration',
    )
    requested = profile.optimality_claim_requested
    if requested == 'global_optimum_proven':
        if (
            exact_family
            and recovered >= 1
            and state == 'qualified'
        ):
            optimality_claim: OptimalityClaim = 'global_optimum_proven'
        else:
            optimality_claim = 'best_found_under_budget'
            reasons.append(
                'global optimality requested but not established — '
                'exact/exhaustive method plus recovered known-optimum '
                'fixtures are required'
            )
            if state == 'qualified':
                state = 'qualified_with_limitations'
            limitations.append(
                'product language must read "best found under declared '
                'search budget"'
            )
    else:
        optimality_claim = 'best_found_under_budget'

    common_fidelity_finalized = all(
        r.common_fidelity_reevaluated for r in profile.runs
    )
    if profile.multi_fidelity is not None:
        mf = profile.multi_fidelity
        if (
            mf.screening_fidelity is not None
            and mf.final_fidelity is not None
            and mf.screening_fidelity != mf.final_fidelity
            and not common_fidelity_finalized
        ):
            limitations.append(
                'finalists were never re-evaluated at the declared '
                'common fidelity'
            )
            if state == 'qualified':
                state = 'qualified_with_limitations'

    if profile.algorithm.surrogate is not None:
        surrogate = profile.algorithm.surrogate
        if (
            surrogate.cross_validation_error is None
            and surrogate.holdout_error is None
        ):
            limitations.append(
                'surrogate-assisted search without retained surrogate '
                'error evidence'
            )
            if state == 'qualified':
                state = 'qualified_with_limitations'

    if not profile.baselines and not profile.known_optimum_fixtures:
        limitations.append(
            'no baseline comparison and no known-optimum fixture — the '
            'search is unbenchmarked'
        )
        if state == 'qualified':
            state = 'qualified_with_limitations'

    for run in profile.runs:
        if run.feasible_count == 0:
            limitations.append(
                f'run {run.run_label} produced zero feasible candidates'
            )
            if state == 'qualified':
                state = 'qualified_with_limitations'

    if state == 'qualified' and limitations:
        state = 'qualified_with_limitations'

    return _seal(
        OptimizationRunQualification,
        {
            'document_id': document_id,
            'profile_ref': run_profile_binding(profile).model_dump(
                mode='json'
            ),
            'state': state,
            'optimality_claim': optimality_claim,
            'convergence_state': convergence_state,
            'run_count': run_count,
            'seed_variability_observed': seed_variability_observed,
            'baselines_compared': len(profile.baselines),
            'known_optimum_recovered': recovered,
            'common_fidelity_finalized': common_fidelity_finalized,
            'regression_flags': sorted(set(regression_flags)),
            'reasons': tuple(reasons),
            'limitations': tuple(dict.fromkeys(limitations)),
            'evaluated_at_utc': evaluated_at_utc,
        },
        'qualification_id',
        'qualification_sha256',
        'optqual',
    )


# ----------------------------------------------------------------------
# Pareto approximation assessment (#675 §12/§13)

class ParetoIndicator(BaseModel):
    """One Pareto-quality indicator with its exact reference binding.

    Each indicator binds the reference point/front it was computed
    against — a number without its reference is not comparable
    (#675 §12).
    """

    model_config = ConfigDict(frozen=True)

    indicator: Literal[
        'hypervolume',
        'generational_distance',
        'inverted_generational_distance',
        'epsilon_indicator',
        'spacing_spread',
        'set_coverage',
        'attainment_surface',
        'nondominated_count',
        'custom_validated',
    ]
    value: float | None = None
    reference_point: str | None = None
    reference_front_ref: AuthorityRef | None = None
    implementation: str | None = None
    implementation_version: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'ParetoIndicator':
        if self.value is not None:
            _require_finite(self.value, 'indicator value')
        _require_ref_sha(
            self.reference_front_ref, 'indicator reference_front_ref'
        )
        reference_indicators = {
            'hypervolume',
            'generational_distance',
            'inverted_generational_distance',
            'epsilon_indicator',
        }
        if self.indicator in reference_indicators:
            if (
                self.indicator == 'hypervolume'
                and self.reference_point is None
            ):
                raise ValueError(
                    'hypervolume requires a declared reference point'
                )
            if self.indicator in {
                'generational_distance',
                'inverted_generational_distance',
                'epsilon_indicator',
            } and self.reference_front_ref is None:
                raise ValueError(
                    f'{self.indicator} requires a pinned reference '
                    'front'
                )
        return self


class ParetoApproximationAssessment(BaseModel):
    """The sealed assessment of one Pareto-front approximation
    (#675 §12/§13).

    Convergence toward a reference, diversity/coverage along the front,
    and constraint feasibility are separate observables — no universal
    Pareto-quality scalar.
    """

    model_config = ConfigDict(frozen=True)

    assessment_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    reference_status: ParetoReferenceStatus
    state: ParetoAssessmentState
    convergence_indicators: tuple[ParetoIndicator, ...] = ()
    diversity_indicators: tuple[ParetoIndicator, ...] = ()
    nondominated_count: int | None = Field(default=None, ge=0)
    feasibility_fraction: float | None = None
    decision_space_diversity: str | None = None
    objective_space_diversity: str | None = None
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(
        default=PARETO_ASSESSMENT_VERSION, min_length=1
    )
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'reference_status': self.reference_status,
            'state': self.state,
            'convergence_indicators': [
                i.model_dump(mode='json')
                for i in self.convergence_indicators
            ],
            'diversity_indicators': [
                i.model_dump(mode='json')
                for i in self.diversity_indicators
            ],
            'nondominated_count': self.nondominated_count,
            'feasibility_fraction': self.feasibility_fraction,
            'decision_space_diversity': self.decision_space_diversity,
            'objective_space_diversity': self.objective_space_diversity,
            'reasons': list(self.reasons),
            'limitations': list(self.limitations),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'ParetoApproximationAssessment':
        _require_iso8601(
            self.evaluated_at_utc, 'assessment evaluated_at_utc'
        )
        if self.profile_ref.kind != 'optimizer_run_profile':
            raise ValueError(
                "profile_ref must pin an 'optimizer_run_profile'"
            )
        _require_ref_sha(self.profile_ref, 'profile_ref')
        if self.feasibility_fraction is not None:
            value = self.feasibility_fraction
            _require_finite(value, 'feasibility_fraction')
            if not (0.0 <= value <= 1.0):
                raise ValueError(
                    'feasibility_fraction must lie in [0, 1]'
                )
        if (
            self.reference_status == 'no_reference_front'
            and self.state == 'assessed_with_reference'
        ):
            raise ValueError(
                'no-reference-front assessments cannot claim '
                'reference-based state'
            )
        if (
            self.reference_status
            in ('exact_reference_front', 'exhaustive_discrete_front')
            and self.state == 'assessed_empirical_only'
        ):
            raise ValueError(
                'exact/exhaustive reference status must not downgrade '
                'to empirical-only'
            )
        expected = _hash(self.identity_payload())
        if self.assessment_sha256 != expected:
            raise ValueError('pareto assessment hash mismatch')
        if self.assessment_id != _semantic_id('parassess', expected):
            raise ValueError(
                'pareto assessment id does not match its hash'
            )
        return self


def pareto_assessment_binding(
    assessment: ParetoApproximationAssessment,
) -> AuthorityRef:
    return AuthorityRef(
        kind='pareto_approximation_assessment',
        ref_id=assessment.assessment_id,
        ref_sha256=assessment.assessment_sha256,
    )


def evaluate_pareto_approximation(
    document_id: str,
    profile: OptimizerRunProfile,
    *,
    reference_status: ParetoReferenceStatus,
    convergence_indicators: Sequence[ParetoIndicator] = (),
    diversity_indicators: Sequence[ParetoIndicator] = (),
    nondominated_count: int | None = None,
    feasibility_fraction: float | None = None,
    decision_space_diversity: str | None = None,
    objective_space_diversity: str | None = None,
    evaluated_at_utc: str | None = None,
) -> ParetoApproximationAssessment:
    """Fail-closed Pareto-approximation assessment (#675 §12/§13).

    - ``no_reference_front`` → at best ``assessed_empirical_only``:
      attainment/coverage evidence is reported, never a front-quality
      scalar.
    - Reference indicators without a declared reference front are
      structurally inconsistent → ``insufficient_evidence``.
    - A ``best_known_aggregate_front`` caps at
      ``assessed_with_limitations`` — a union of optimizer outputs is
      never the true front.
    - Zero nondominated solutions → ``insufficient_evidence``.
    """

    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')

    if not profile.multi_objective:
        raise ValueError(
            'pareto assessment requires a multi-objective run profile'
        )

    reasons: list[str] = []
    limitations: list[str] = []

    reference_fronts = {
        'exact_reference_front',
        'exhaustive_discrete_front',
        'best_known_aggregate_front',
    }
    if reference_status == 'no_reference_front':
        reference_indicators = [
            i
            for i in convergence_indicators
            if i.indicator
            in {
                'generational_distance',
                'inverted_generational_distance',
                'epsilon_indicator',
            }
        ]
        if reference_indicators:
            state: ParetoAssessmentState = 'insufficient_evidence'
            reasons.append(
                'reference-front indicators declared without a '
                'reference front'
            )
        elif not convergence_indicators and not diversity_indicators:
            state = 'insufficient_evidence'
            reasons.append(
                'no convergence or diversity evidence was declared'
            )
        else:
            state = 'assessed_empirical_only'
            limitations.append(
                'no reference front — report empirical '
                'attainment/coverage behavior only'
            )
    elif reference_status == 'best_known_aggregate_front':
        state = 'assessed_with_limitations'
        limitations.append(
            'reference front is a best-known aggregate — a union of '
            'optimizer outputs is not the true Pareto front'
        )
    elif not convergence_indicators:
        state = 'insufficient_evidence'
        reasons.append(
            'a reference front exists but no convergence indicator was '
            'computed against it'
        )
    else:
        state = 'assessed_with_reference'

    if nondominated_count == 0:
        state = 'insufficient_evidence'
        reasons.append(
            'zero nondominated solutions — nothing to assess'
        )

    if feasibility_fraction is not None and feasibility_fraction < 1.0:
        limitations.append(
            'infeasible members present in the evaluated set'
        )

    if not diversity_indicators:
        limitations.append(
            'no diversity/coverage indicator — convergence alone does '
            'not qualify a front'
        )
        if state == 'assessed_with_reference':
            state = 'assessed_with_limitations'

    return _seal(
        ParetoApproximationAssessment,
        {
            'document_id': document_id,
            'profile_ref': run_profile_binding(profile).model_dump(
                mode='json'
            ),
            'reference_status': reference_status,
            'state': state,
            'convergence_indicators': [
                i.model_dump(mode='json') for i in convergence_indicators
            ],
            'diversity_indicators': [
                i.model_dump(mode='json') for i in diversity_indicators
            ],
            'nondominated_count': nondominated_count,
            'feasibility_fraction': feasibility_fraction,
            'decision_space_diversity': decision_space_diversity,
            'objective_space_diversity': objective_space_diversity,
            'reasons': tuple(reasons),
            'limitations': tuple(dict.fromkeys(limitations)),
            'evaluated_at_utc': evaluated_at_utc,
        },
        'assessment_id',
        'assessment_sha256',
        'parassess',
    )
