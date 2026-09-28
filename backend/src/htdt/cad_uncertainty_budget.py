"""Measurement & prediction uncertainty budget authority (#979).

HTDT already stores individual uncertainty evidence (pose tolerance,
level-calibration uncertainty, repeatability spreads, numerical error
bounds, #140/O90 bounded-vs-distribution semantics). What was missing is
the authority that **combines compatible uncertainties through one
declared model** for one exact measurand — without false precision.

- ``UncertaintyComponent`` — one typed input quantity with an explicit
  category and representation. ``bounded_interval`` is an engineering
  bound; it is never silently promoted to a Gaussian standard deviation.
- ``UncertaintyBudgetSpec`` — binds the exact measurand, the exact
  prediction/measurement/comparison model, the component list and the
  declared propagation method.
- ``UncertaintyBudgetResult`` — the propagated outcome: standard
  uncertainty, expanded interval or empirical summary, per-component
  contributions, and limitations — with model-discrepancy components kept
  strictly separate from measurement noise.
- deterministic propagators: first-order linear (root-sum-of-squares of
  standard uncertainties times sensitivities) and bounded worst-case
  (linear sum of bounds), plus a ``declared_only`` pass-through when no
  honest combination exists.
"""

from __future__ import annotations

from math import isfinite, sqrt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash, canonicalize_payload






_SHA256_PATTERN = r'^[0-9a-f]{64}$'


UncertaintyCategory = Literal[
    'measurement_instrument',
    'spatial_operating_condition',
    'model_input',
    'numerical',
    'model_discrepancy',
    'unknown',
]
"""Categories are never collapsed into one another — in particular a
``model_discrepancy`` component is never folded into measurement noise."""

UncertaintyRepresentation = Literal[
    'bounded_interval',
    'empirical_samples',
    'standard_uncertainty',
    'distribution',
    'covariance',
    'discrete_alternatives',
    'unknown',
]
"""``±`` bounds are intervals, not distributions: converting one requires
a declared assumption, never a silent Gaussian."""

PropagationMethod = Literal[
    'first_order_linear',
    'monte_carlo',
    'bounded_worst_case',
    'hybrid',
    'declared_only',
    'unknown',
]

BudgetCombinationState = Literal[
    'propagated', 'partial', 'declared_only', 'unsupported', 'unknown'
]


class UncertaintyComponent(BaseModel):
    """One input-quantity uncertainty with honest representation."""

    model_config = ConfigDict(frozen=True)

    component_id: str = Field(min_length=1)
    subject_quantity: str = Field(min_length=1)
    subject_authority_ref: str | None = None
    quantity_unit: str | None = None
    quantity_domain: Literal['frequency', 'time', 'spatial', 'level', 'other', 'unknown'] = 'unknown'
    category: UncertaintyCategory = 'unknown'
    representation: UncertaintyRepresentation = 'unknown'
    bound_low: float | None = None
    bound_high: float | None = None
    standard_uncertainty: float | None = Field(default=None, ge=0.0)
    empirical_samples: tuple[float, ...] | None = None
    distribution_json: str | None = None
    covariance_ref: str | None = None
    discrete_alternatives: tuple[str, ...] | None = None
    coverage_probability: float | None = None
    coverage_semantics: str | None = None
    correlation_group_id: str | None = None
    applicability: str | None = None
    sensitivity: float | None = None
    evidence_refs: tuple[str, ...] = ()
    provenance_json: str = '{}'

    @model_validator(mode='after')
    def valid_component(self) -> 'UncertaintyComponent':
        if self.category == 'unknown':
            raise ValueError('uncertainty component requires an explicit category')
        if self.representation == 'unknown':
            raise ValueError('uncertainty component requires an explicit representation')
        if self.representation == 'bounded_interval':
            if self.bound_low is None or self.bound_high is None:
                raise ValueError('bounded_interval requires bound_low/bound_high')
            if not (
                isfinite(float(self.bound_low))
                and isfinite(float(self.bound_high))
            ) or self.bound_high < self.bound_low:
                raise ValueError('bounded interval must satisfy low <= high')
        if self.representation == 'standard_uncertainty':
            if self.standard_uncertainty is None:
                raise ValueError('standard_uncertainty representation requires the value')
            if not isfinite(float(self.standard_uncertainty)):
                raise ValueError('standard_uncertainty must be finite')
        if self.representation == 'empirical_samples':
            if not self.empirical_samples:
                raise ValueError('empirical_samples requires at least one sample')
            if any(not isfinite(float(v)) for v in self.empirical_samples):
                raise ValueError('empirical samples must be finite')
        if self.coverage_probability is not None and not (
            0.0 < float(self.coverage_probability) <= 1.0
        ):
            raise ValueError('coverage_probability must lie in (0, 1]')
        if self.sensitivity is not None and not isfinite(float(self.sensitivity)):
            raise ValueError('sensitivity must be finite')
        return self


class UncertaintyBudgetSpec(BaseModel):
    """Exact model + component set the budget is computed under."""

    model_config = ConfigDict(frozen=True)

    spec_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    measurand: str = Field(min_length=1)
    measurand_unit: str | None = None
    model_ref: str | None = None
    model_version: str | None = None
    scene_revision_id: str | None = None
    components: tuple[UncertaintyComponent, ...] = ()
    propagation_method: PropagationMethod = 'unknown'
    correlation_policy: Literal[
        'independent_unless_declared', 'correlation_groups', 'unknown'
    ] = 'independent_unless_declared'
    output_semantics: str | None = None
    created_at_utc: str = Field(min_length=1)
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_spec(self) -> 'UncertaintyBudgetSpec':
        ids = [component.component_id for component in self.components]
        if len(ids) != len(set(ids)):
            raise ValueError('component ids must be unique')
        if self.propagation_method == 'unknown':
            raise ValueError('budget spec requires an explicit propagation method')
        if self.propagation_method == 'first_order_linear':
            for component in self.components:
                if component.representation not in (
                    'standard_uncertainty',
                    'empirical_samples',
                ):
                    raise ValueError(
                        'first_order_linear propagation requires standard '
                        'uncertainties (or empirical samples reduced under a '
                        'declared rule); bounded intervals need '
                        'bounded_worst_case or a declared distribution — '
                        'never an assumed Gaussian'
                    )
        if self.spec_sha256 != _hash(self.identity_payload()):
            raise ValueError('uncertainty budget spec hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'spec_sha256'})


class ComponentContribution(BaseModel):
    model_config = ConfigDict(frozen=True)

    component_id: str = Field(min_length=1)
    category: UncertaintyCategory
    contribution: float = Field(ge=0.0)
    representation: UncertaintyRepresentation


class UncertaintyBudgetResult(BaseModel):
    """Propagated uncertainty for one measurand under one exact spec.

    ``model_discrepancy_contribution`` is reported separately — model
    error is never summed into measurement uncertainty.
    """

    model_config = ConfigDict(frozen=True)

    result_id: str = Field(min_length=1)
    spec_id: str = Field(min_length=1)
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    measurand: str = Field(min_length=1)
    combination_state: BudgetCombinationState = 'unknown'
    combined_standard_uncertainty: float | None = Field(default=None, ge=0.0)
    combined_bound_half_width: float | None = Field(default=None, ge=0.0)
    expanded_uncertainty: float | None = Field(default=None, ge=0.0)
    coverage_factor: float | None = None
    contributions: tuple[ComponentContribution, ...] = ()
    model_discrepancy_contribution: float | None = Field(default=None, ge=0.0)
    limitations: tuple[str, ...] = ()
    created_at_utc: str = Field(min_length=1)
    result_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_result(self) -> 'UncertaintyBudgetResult':
        for value in (
            self.combined_standard_uncertainty,
            self.combined_bound_half_width,
            self.expanded_uncertainty,
            self.coverage_factor,
            self.model_discrepancy_contribution,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('budget result values must be finite')
        if self.combination_state == 'propagated' and (
            self.combined_standard_uncertainty is None
            and self.combined_bound_half_width is None
        ):
            raise ValueError(
                'a propagated result requires a combined standard uncertainty '
                'or a combined bound'
            )
        if self.expanded_uncertainty is not None and self.coverage_factor is None:
            raise ValueError('expanded_uncertainty requires a coverage_factor')
        if self.result_sha256 != _hash(self.identity_payload()):
            raise ValueError('uncertainty budget result hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'result_sha256'})


def build_uncertainty_budget_spec(**kwargs: Any) -> UncertaintyBudgetSpec:
    """Assemble and seal an :class:`UncertaintyBudgetSpec`."""
    payload = {'spec_sha256': '0' * 64, **kwargs}
    provisional = UncertaintyBudgetSpec.model_construct(**canonicalize_payload(UncertaintyBudgetSpec, dict(**payload)))
    payload['spec_sha256'] = _hash(provisional.identity_payload())
    return UncertaintyBudgetSpec(**payload)


def _component_std(component: UncertaintyComponent) -> float:
    """Standard-uncertainty view of a component under its own semantics.

    Only ``standard_uncertainty`` and ``empirical_samples`` (sample
    standard deviation) produce one honestly; everything else raises so a
    bound is never silently read as a Gaussian sigma.
    """
    if component.representation == 'standard_uncertainty':
        return float(component.standard_uncertainty)  # type: ignore[arg-type]
    if component.representation == 'empirical_samples':
        samples = tuple(float(v) for v in component.empirical_samples or ())
        if len(samples) < 2:
            raise ValueError('empirical standard uncertainty needs >= 2 samples')
        mean = sum(samples) / len(samples)
        return sqrt(sum((v - mean) ** 2 for v in samples) / (len(samples) - 1))
    raise ValueError(
        f'component {component.component_id!r} representation '
        f'{component.representation!r} has no honest standard uncertainty'
    )


def _component_half_width(component: UncertaintyComponent) -> float:
    if component.representation != 'bounded_interval':
        raise ValueError(
            'bounded worst-case propagation requires bounded_interval '
            'components'
        )
    return (float(component.bound_high) - float(component.bound_low)) / 2.0  # type: ignore[arg-type]


def propagate_uncertainty_budget(
    spec: UncertaintyBudgetSpec,
    *,
    result_id: str,
    created_at_utc: str,
    coverage_factor: float | None = None,
) -> UncertaintyBudgetResult:
    """Propagate the spec's components under its declared method.

    ``first_order_linear``: combined standard uncertainty =
    root-sum-of-squares of per-component ``u * sensitivity`` for
    uncorrelated components; components sharing a ``correlation_group_id``
    sum linearly inside the group before quadrature — declared correlation
    is honored, never assumed.
    ``bounded_worst_case``: linear sum of half-widths × sensitivity.
    Model-discrepancy components are always reported separately.
    """
    ordinary = [
        c for c in spec.components if c.category != 'model_discrepancy'
    ]
    discrepancy = [
        c for c in spec.components if c.category == 'model_discrepancy'
    ]
    contributions: list[ComponentContribution] = []
    limitations: list[str] = []
    combined_std: float | None = None
    combined_bound: float | None = None
    discrepancy_value: float | None = None
    expanded: float | None = None
    state: BudgetCombinationState

    for component in discrepancy:
        try:
            discrepancy_value = (
                (discrepancy_value or 0.0) + _component_half_width(component)
                if component.representation == 'bounded_interval'
                else (discrepancy_value or 0.0)
                + _component_std(component)
            )
        except ValueError:
            discrepancy_value = None
            limitations.append(
                f'model_discrepancy {component.component_id} is declared '
                'but not reducible under the budget method'
            )

    if spec.propagation_method == 'first_order_linear':
        groups: dict[str, list[float]] = {}
        for component in ordinary:
            u = _component_std(component) * abs(
                component.sensitivity if component.sensitivity is not None else 1.0
            )
            # a missing correlation_group_id means *independent*, not
            # "shares a group with every other undeclared component"
            group_key = (
                component.correlation_group_id
                if component.correlation_group_id is not None
                else f'__independent__:{component.component_id}'
            )
            groups.setdefault(group_key, []).append(u)
            contributions.append(
                ComponentContribution(
                    component_id=component.component_id,
                    category=component.category,
                    contribution=u,
                    representation=component.representation,
                )
            )
        total_sq = 0.0
        for _group, values in groups.items():
            # declared correlation inside a group sums linearly; groups
            # combine in quadrature
            total_sq += (sum(values)) ** 2
        combined_std = sqrt(total_sq)
        state = 'propagated'
    elif spec.propagation_method == 'bounded_worst_case':
        total = 0.0
        for component in ordinary:
            half = _component_half_width(component) * abs(
                component.sensitivity if component.sensitivity is not None else 1.0
            )
            total += half
            contributions.append(
                ComponentContribution(
                    component_id=component.component_id,
                    category=component.category,
                    contribution=half,
                    representation=component.representation,
                )
            )
        combined_bound = total
        state = 'propagated'
    elif spec.propagation_method == 'declared_only':
        state = 'declared_only'
        limitations.append(
            'components retained without a combined statistic; the budget '
            'declares inputs but does not invent a propagation'
        )
    else:
        raise ValueError(
            f'propagation method {spec.propagation_method!r} is not '
            'implemented — use declared_only for honest pass-through'
        )

    if combined_std is not None and coverage_factor is not None:
        if not isfinite(float(coverage_factor)) or coverage_factor <= 0:
            raise ValueError('coverage_factor must be positive')
        expanded = combined_std * float(coverage_factor)

    payload: dict[str, Any] = {
        'result_id': result_id,
        'spec_id': spec.spec_id,
        'spec_sha256': spec.spec_sha256,
        'document_id': spec.document_id,
        'measurand': spec.measurand,
        'combination_state': state,
        'combined_standard_uncertainty': combined_std,
        'combined_bound_half_width': combined_bound,
        'expanded_uncertainty': expanded,
        'coverage_factor': coverage_factor,
        'contributions': tuple(contributions),
        'model_discrepancy_contribution': discrepancy_value,
        'limitations': tuple(limitations),
        'created_at_utc': created_at_utc,
        'result_sha256': '0' * 64,
    }
    provisional = UncertaintyBudgetResult.model_construct(**canonicalize_payload(UncertaintyBudgetResult, dict(**payload)))
    payload['result_sha256'] = _hash(provisional.identity_payload())
    return UncertaintyBudgetResult(**payload)
