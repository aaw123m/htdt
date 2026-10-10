"""Numerical solver reproducibility authority (issue #703).

A solver that is converged/accurate in principle can still differ
run-to-run or platform-to-platform because of floating-point reduction
order, parallel scheduling, stochastic ray sampling, seeds, math
library/compiler behaviour or precision mode. A candidate difference
smaller than the solver's own numerical variability must NOT be
reported as deterministic engineering truth.

Basis: Demmel & Nguyen, "Parallel Reproducible Summation", IEEE TC
64(7), 2015 (reduction-order dependence; order-independent
reproducible summation); ReproBLAS (reproducibility needs explicit
algorithms); Iakymchuk et al., JCAM 371, 2020 (reproducible PCG via
explicit reproducible reductions); GPU run-to-run variability
literature (roundoff + non-associativity + nondeterministic
scheduling; ensemble/CI treatment).
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...cad_authority_registry import AuthorityRef
from ...canonical_json import canonical_sha256 as _hash, canonicalize_payload

_SHA256_PATTERN = r'^[0-9a-f]{64}$'

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
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )

_PRECISION_KINDS = (
    'fp32', 'fp64', 'extended', 'mixed', 'unknown',
)
_PARALLELISM_KINDS = (
    'serial', 'threaded', 'distributed', 'gpu', 'hybrid', 'unknown',
)
_SEED_POLICIES = (
    # 'fixed' — identical seed each run; 'per_run' — a different
    # recorded seed each run; 'unknown' — seed behaviour not declared.
    'fixed', 'per_run', 'unknown',
)
_REALIZATION_KINDS = (
    'deterministic_run', 'stochastic_realization', 'ensemble_member',
)
_COMPARISON_DOMAINS = (
    'same_platform_same_config',
    'same_platform_different_parallelism',
    'cross_platform',
    'cross_math_library',
)

NumericalVerdict = Literal[
    'deterministic_order',            # difference > variability envelope
    'within_numerical_variability',   # difference inside run-to-run band
    'insufficient_realizations',      # variability never characterized
    'not_comparable',                 # no reproducibility basis at all
]

class NumericalReproducibilityProfile(BaseModel):
    """How a solver run intends to reproduce numerically (#703).

    A parallel/distributed/GPU run without a declared reproducible
    reduction strategy cannot promise run-to-run bit-consistency.
    A solver without a seed policy cannot promise stochastic
    reproducibility either.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    solver_ref: AuthorityRef
    precision_kind: Literal[
        'fp32', 'fp64', 'extended', 'mixed', 'unknown'
    ]
    parallelism_kind: Literal[
        'serial', 'threaded', 'distributed', 'gpu', 'hybrid', 'unknown'
    ]
    reduction_order_pinned: bool = False
    reproducible_reduction_ref: AuthorityRef | None = None
    math_library_ref: AuthorityRef | None = None
    seed_policy: Literal['fixed', 'per_run', 'unknown'] = 'unknown'

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('precision_kind') not in _PRECISION_KINDS:
                raise ValueError('unknown precision kind')
            if data.get('parallelism_kind') not in _PARALLELISM_KINDS:
                raise ValueError('unknown parallelism kind')
            if data.get('seed_policy') not in _SEED_POLICIES:
                raise ValueError('unknown seed policy')
            if (
                data.get('reduction_order_pinned')
                and data.get('reproducible_reduction_ref') is None
            ):
                raise ValueError(
                    'pinned reduction order requires a reproducible-'
                    'reduction reference (e.g. ReproBLAS)'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'NumericalReproducibilityProfile':
        return _seal(
            cls, payload, 'profile_id', 'profile_sha256', 'nrep'
        )

class StochasticRealizationRecord(BaseModel):
    """One realization of a solver run under a pinned profile.

    Repeat realizations under 'deterministic_run' are only honest when
    the profile actually pins order/seeds; stochastic realizations are
    ensemble members of the same configuration.
    """

    model_config = ConfigDict(frozen=True)

    record_id: str
    record_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    realization_kind: Literal[
        'deterministic_run', 'stochastic_realization', 'ensemble_member'
    ]
    seed: int | None = None
    sample_count: int | None = None
    result_ref: AuthorityRef | None = None
    platform_descriptor: str | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            kind = data.get('realization_kind')
            if kind not in _REALIZATION_KINDS:
                raise ValueError('unknown realization kind')
            if (
                kind in ('stochastic_realization', 'ensemble_member')
                and data.get('seed') is None
            ):
                raise ValueError(
                    'stochastic realizations must record their seed'
                )
            if data.get('result_ref') is None:
                raise ValueError(
                    'a realization without a pinned result is not evidence'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'record_id', 'record_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'StochasticRealizationRecord':
        return _seal(
            cls, payload, 'record_id', 'record_sha256', 'srez'
        )

class CrossPlatformNumericalComparison(BaseModel):
    """Measured run-to-run / cross-platform numerical comparison (#703).

    Binds the realization refs compared, the comparison domain, and the
    observed maximum deviation in each compared quantity. This is the
    variability envelope every downstream ordering claim must respect.
    """

    model_config = ConfigDict(frozen=True)

    comparison_id: str
    comparison_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    domain: Literal[
        'same_platform_same_config',
        'same_platform_different_parallelism',
        'cross_platform',
        'cross_math_library',
    ]
    realization_refs: tuple[AuthorityRef, ...]
    metric_deviations: dict[str, float]
    # Ensemble uncertainty descriptor (e.g. CI half-width per metric)
    # when stochastic realizations are compared.
    ensemble_uncertainty: dict[str, float] | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('domain') not in _COMPARISON_DOMAINS:
                raise ValueError('unknown comparison domain')
            refs = data.get('realization_refs') or ()
            if len(refs) < 2:
                raise ValueError(
                    'a numerical comparison requires >= 2 realizations'
                )
            dev = data.get('metric_deviations') or {}
            if not dev:
                raise ValueError(
                    'a comparison without measured deviations is '
                    'not evidence of variability'
                )
            if any(v < 0 for v in dev.values()):
                raise ValueError('deviations must be non-negative')
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'comparison_id', 'comparison_sha256'},
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'CrossPlatformNumericalComparison':
        return _seal(
            cls, payload, 'comparison_id', 'comparison_sha256', 'nxcmp'
        )

def _require_refs(*refs: AuthorityRef | None) -> None:
    for ref in refs:
        if ref is None or ref.ref_sha256 is None:
            raise ValueError('authority references must be sha-pinned')

def evaluate_numerical_difference_claim(
    declared_difference: float | None,
    comparison: CrossPlatformNumericalComparison | None,
    metric: str,
    *,
    profile: NumericalReproducibilityProfile | None = None,
) -> tuple[NumericalVerdict, str]:
    """Judge whether a declared candidate difference is a deterministic
    ordering or just numerical variability (#703).

    - no comparison → 'not_comparable'
    - metric absent from the comparison → 'insufficient_realizations'
    - |difference| <= measured deviation → 'within_numerical_variability'
    - |difference| > deviation → 'deterministic_order' only when the
      profile pins reproducibility (serial or pinned reduction + fixed
      seed, or explicit reproducible-reduction ref); otherwise the
      larger difference is still evidence but reported honestly.
    """
    if comparison is None:
        return 'not_comparable', 'no numerical comparison evidence'
    if metric not in comparison.metric_deviations:
        return (
            'insufficient_realizations',
            f'metric {metric} was never characterized run-to-run',
        )
    if declared_difference is None:
        return (
            'insufficient_realizations',
            'no declared difference to judge',
        )
    deviation = comparison.metric_deviations[metric]
    margin = 0.0
    if comparison.ensemble_uncertainty is not None:
        margin = comparison.ensemble_uncertainty.get(metric, 0.0)
    band = deviation + margin
    if abs(declared_difference) <= band:
        return (
            'within_numerical_variability',
            f'difference {declared_difference} inside run-to-run band '
            f'{band}',
        )
    if profile is not None:
        deterministic_basis = (
            profile.parallelism_kind == 'serial'
            or profile.reduction_order_pinned
        ) and profile.seed_policy != 'unknown'
        if not deterministic_basis:
            return (
                'within_numerical_variability',
                'profile does not pin reduction order/seed — a larger '
                'difference still cannot be claimed deterministic',
            )
    return (
        'deterministic_order',
        f'difference {declared_difference} exceeds variability band '
        f'{band}',
    )

NUMERICAL_LABELS: dict[str, str] = {
    'deterministic_order': '確定的順序（変動帯域を超過）',
    'within_numerical_variability': '数値変動範囲内（順序は主張不可）',
    'insufficient_realizations': '変動の実測不足',
    'not_comparable': '数値比較の根拠なし',
}
