"""Treatment search specification + deterministic enumeration (#516).

A ``TreatmentSearchSpec`` declares the bounded discrete space the optimizer
may explore (candidate host surfaces/regions, allowed treatment definition
authorities, count limits), its hard constraints (excluded surfaces,
coverage, protrusion, spacing, budget via the shared #514 cost-scenario
authority), independent objective definitions and the exact solver/provider
capability envelope. ``enumerate_treatment_candidates`` deterministically
materializes the discrete space — candidates never rewrite host base
materials and carry only canonical definition/placement references, never
an optimizer-only material schema.
"""

from __future__ import annotations

from hashlib import sha256
from itertools import combinations
import json
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_repository import SceneRevision
from .cad_system_variant import SystemVariant
from .optimization_objectives import ObjectiveDefinition
from .optimization_robustness import UncertaintyAxis


TREATMENT_SEARCH_SCHEMA_VERSION = 1
TREATMENT_SEARCH_AUTHORITY_VERSION = 'treatment-search-1'
DEFAULT_TREATMENT_SEARCH_ALGORITHM = 'treatment-search-enumeration-1'


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


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


class TreatmentCandidateSlot(BaseModel):
    """One discrete placement option: host surface + optional region cell."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    host_surface_id: str = Field(min_length=1)
    region_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_slot(self) -> 'TreatmentCandidateSlot':
        return self


class TreatmentAssignment(BaseModel):
    """Choice of one treatment definition authority at one candidate slot."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    slot: TreatmentCandidateSlot
    definition_id: str = Field(min_length=1)
    definition_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    quantity: int = Field(default=1, ge=1, le=64)


class TreatmentSearchSpace(BaseModel):
    """Bounded discrete variable space — no unconstrained Cartesian axes."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    allowed_definition_ids: tuple[str, ...] = Field(min_length=1)
    candidate_slots: tuple[TreatmentCandidateSlot, ...] = Field(min_length=1)
    max_treatment_count: int = Field(ge=1, le=512)
    include_empty_baseline: bool = True

    @model_validator(mode='after')
    def valid_space(self) -> 'TreatmentSearchSpace':
        if len(set(self.allowed_definition_ids)) != len(
            self.allowed_definition_ids
        ):
            raise ValueError('allowed treatment definition ids must be unique')
        slot_keys = [
            (item.host_surface_id, item.region_id)
            for item in self.candidate_slots
        ]
        if len(slot_keys) != len(set(slot_keys)):
            raise ValueError('candidate slots must be unique')
        if self.max_treatment_count > len(self.candidate_slots):
            raise ValueError(
                'max treatment count cannot exceed the candidate slot count'
            )
        return self


class TreatmentHardConstraints(BaseModel):
    """Feasibility limits — violations are infeasible evidence, not penalties."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    excluded_surface_ids: tuple[str, ...] = ()
    max_coverage_m2: float | None = Field(default=None, gt=0.0)
    max_protrusion_m: float | None = Field(default=None, gt=0.0)
    min_spacing_m: float | None = Field(default=None, ge=0.0)
    budget_ceiling: float | None = Field(default=None, ge=0.0)
    cost_scenario_id: str | None = Field(default=None, min_length=1)
    cost_scenario_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )

    @model_validator(mode='after')
    def valid_constraints(self) -> 'TreatmentHardConstraints':
        for value in (
            self.max_coverage_m2,
            self.max_protrusion_m,
            self.min_spacing_m,
            self.budget_ceiling,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('hard constraints must be finite numbers')
        has_scenario = (
            self.cost_scenario_id is not None
            or self.cost_scenario_sha256 is not None
        )
        if has_scenario and not (
            self.cost_scenario_id and self.cost_scenario_sha256
        ):
            raise ValueError(
                'cost scenario id and sha256 must be supplied together'
            )
        if self.budget_ceiling is not None and self.cost_scenario_id is None:
            raise ValueError(
                'a budget ceiling requires an exact cost scenario authority'
            )
        return self


class TreatmentSolverCapability(BaseModel):
    """Explicit provider physics envelope — unsupported stays unavailable."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    provider_id: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    supported_physics: tuple[str, ...] = Field(min_length=1)
    usable_band_hz: tuple[float, float]

    @model_validator(mode='after')
    def valid_capability(self) -> 'TreatmentSolverCapability':
        if len(set(self.supported_physics)) != len(self.supported_physics):
            raise ValueError('supported physics labels must be unique')
        low, high = self.usable_band_hz
        if (
            not isfinite(float(low))
            or not isfinite(float(high))
            or low <= 0.0
            or high <= low
        ):
            raise ValueError('usable band must be a finite ordered range')
        return self


class TreatmentSearchSpec(BaseModel):
    """Immutable treatment-search contract bound to exact authorities."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = TREATMENT_SEARCH_SCHEMA_VERSION
    authority_version: Literal[
        'treatment-search-1'
    ] = TREATMENT_SEARCH_AUTHORITY_VERSION

    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    base_system_variant_id: str = Field(min_length=1)
    base_system_variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    space: TreatmentSearchSpace
    hard_constraints: TreatmentHardConstraints = TreatmentHardConstraints()
    solver_capability: TreatmentSolverCapability
    metric_definitions: tuple[ObjectiveDefinition, ...] = Field(min_length=1)
    robustness_axes: tuple[UncertaintyAxis, ...] = ()
    candidate_budget: int = Field(ge=1, le=500_000)
    algorithm_version: str = Field(min_length=1)

    spec_id: str = Field(min_length=1)
    spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_spec(self) -> 'TreatmentSearchSpec':
        excluded = set(self.hard_constraints.excluded_surface_ids)
        for slot in self.space.candidate_slots:
            if slot.host_surface_id in excluded:
                raise ValueError(
                    'candidate slots may not target excluded host surfaces; '
                    'remove them from the search space instead'
                )
        objective_ids = [item.objective_id for item in self.metric_definitions]
        if len(objective_ids) != len(set(objective_ids)):
            raise ValueError('metric definitions must be unique by objective_id')
        axis_ids = [item.axis_id for item in self.robustness_axes]
        if len(axis_ids) != len(set(axis_ids)):
            raise ValueError('robustness axes must be unique')
        if self.spec_sha256 != _digest(self.identity_payload()):
            raise ValueError('treatment search spec hash mismatch')
        if self.spec_id != _semantic_id('treatment-search', self.spec_sha256):
            raise ValueError('treatment search spec ID mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'base_system_variant_id': self.base_system_variant_id,
            'base_system_variant_sha256': self.base_system_variant_sha256,
            'space': self.space.model_dump(mode='json'),
            'hard_constraints': self.hard_constraints.model_dump(mode='json'),
            'solver_capability': self.solver_capability.model_dump(mode='json'),
            'metric_definitions': [
                item.model_dump(mode='json')
                for item in self.metric_definitions
            ],
            'robustness_axes': [
                item.model_dump(mode='json') for item in self.robustness_axes
            ],
            'candidate_budget': self.candidate_budget,
            'algorithm_version': self.algorithm_version,
            'created_at_utc': self.created_at_utc,
        }


def build_treatment_search_spec(
    *,
    scene_revision: SceneRevision,
    base_variant: SystemVariant,
    space: TreatmentSearchSpace,
    solver_capability: TreatmentSolverCapability,
    metric_definitions: Sequence[ObjectiveDefinition],
    candidate_budget: int,
    created_at_utc: str,
    hard_constraints: TreatmentHardConstraints | None = None,
    robustness_axes: Sequence[UncertaintyAxis] = (),
    algorithm_version: str = DEFAULT_TREATMENT_SEARCH_ALGORITHM,
) -> TreatmentSearchSpec:
    if (
        base_variant.document_id != scene_revision.document_id
        or base_variant.baseline_revision_id != scene_revision.revision_id
        or base_variant.baseline_content_hash != scene_revision.content_hash
    ):
        raise ValueError(
            'treatment search baseline variant must bind the exact SceneRevision'
        )
    constraints = hard_constraints or TreatmentHardConstraints()
    identity: dict[str, Any] = {
        'schema_version': TREATMENT_SEARCH_SCHEMA_VERSION,
        'authority_version': TREATMENT_SEARCH_AUTHORITY_VERSION,
        'document_id': scene_revision.document_id,
        'scene_revision_id': scene_revision.revision_id,
        'scene_content_hash': scene_revision.content_hash,
        'base_system_variant_id': base_variant.variant_id,
        'base_system_variant_sha256': base_variant.variant_sha256,
        'space': space.model_dump(mode='json'),
        'hard_constraints': constraints.model_dump(mode='json'),
        'solver_capability': solver_capability.model_dump(mode='json'),
        'metric_definitions': [
            item.model_dump(mode='json') for item in metric_definitions
        ],
        'robustness_axes': [
            item.model_dump(mode='json') for item in robustness_axes
        ],
        'candidate_budget': candidate_budget,
        'algorithm_version': algorithm_version,
        'created_at_utc': created_at_utc,
    }
    digest = _digest(identity)
    return TreatmentSearchSpec(
        document_id=scene_revision.document_id,
        scene_revision_id=scene_revision.revision_id,
        scene_content_hash=scene_revision.content_hash,
        base_system_variant_id=base_variant.variant_id,
        base_system_variant_sha256=base_variant.variant_sha256,
        space=space,
        hard_constraints=constraints,
        solver_capability=solver_capability,
        metric_definitions=tuple(metric_definitions),
        robustness_axes=tuple(robustness_axes),
        candidate_budget=candidate_budget,
        algorithm_version=algorithm_version,
        spec_id=_semantic_id('treatment-search', digest),
        spec_sha256=digest,
        created_at_utc=created_at_utc,
    )


class TreatmentSearchCandidate(BaseModel):
    """One deterministic enumeration member — never scene truth."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = TREATMENT_SEARCH_SCHEMA_VERSION
    spec_id: str = Field(min_length=1)
    spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_index: int = Field(ge=0)
    assignments: tuple[TreatmentAssignment, ...] = ()
    candidate_id: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_candidate(self) -> 'TreatmentSearchCandidate':
        slot_keys = [
            (item.slot.host_surface_id, item.slot.region_id)
            for item in self.assignments
        ]
        if len(slot_keys) != len(set(slot_keys)):
            raise ValueError('candidate assignments must target distinct slots')
        if self.candidate_sha256 != _digest(self.identity_payload()):
            raise ValueError('treatment candidate hash mismatch')
        if self.candidate_id != _semantic_id(
            'treatment-candidate', self.candidate_sha256
        ):
            raise ValueError('treatment candidate ID mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'spec_id': self.spec_id,
            'spec_sha256': self.spec_sha256,
            'candidate_index': self.candidate_index,
            'assignments': [
                item.model_dump(mode='json') for item in self.assignments
            ],
        }


def _candidate_sort_key(item: TreatmentSearchCandidate) -> tuple:
    return (
        len(item.assignments),
        tuple(
            (
                a.slot.host_surface_id,
                a.slot.region_id or '',
                a.definition_id,
                a.quantity,
            )
            for a in item.assignments
        ),
        item.candidate_index,
    )


def enumerate_treatment_candidates(
    spec: TreatmentSearchSpec,
    *,
    definition_sha256s: dict[str, str] | None = None,
) -> list[TreatmentSearchCandidate]:
    """Materialize the discrete space deterministically.

    Canonical order: assignment count, then slot/definition keys. When the
    full space exceeds ``spec.candidate_budget`` the canonically-ordered
    prefix is returned — the spec is deterministic for identical inputs.
    """
    slots = sorted(
        spec.space.candidate_slots,
        key=lambda s: (s.host_surface_id, s.region_id or ''),
    )
    definitions = sorted(spec.space.allowed_definition_ids)
    max_count = spec.space.max_treatment_count

    raw: list[tuple[TreatmentAssignment, ...]] = []
    if spec.space.include_empty_baseline:
        raw.append(())
    for count in range(1, max_count + 1):
        for slot_combo in combinations(slots, count):
            combos = [()] 
            for slot in slot_combo:
                combos = [
                    combo + (
                        TreatmentAssignment(
                            slot=slot,
                            definition_id=definition_id,
                            definition_sha256=(
                                definition_sha256s.get(definition_id)
                                if definition_sha256s
                                else None
                            ),
                        ),
                    )
                    for combo in combos
                    for definition_id in definitions
                ]
            raw.extend(combos)

    members = []
    for index, assignments in enumerate(raw):
        identity: dict[str, Any] = {
            'schema_version': TREATMENT_SEARCH_SCHEMA_VERSION,
            'spec_id': spec.spec_id,
            'spec_sha256': spec.spec_sha256,
            'candidate_index': index,
            'assignments': [item.model_dump(mode='json') for item in assignments],
        }
        digest = _digest(identity)
        members.append(
            TreatmentSearchCandidate(
                spec_id=spec.spec_id,
                spec_sha256=spec.spec_sha256,
                candidate_index=index,
                assignments=assignments,
                candidate_id=_semantic_id('treatment-candidate', digest),
                candidate_sha256=digest,
            )
        )
    members.sort(key=_candidate_sort_key)
    return members[: spec.candidate_budget]


def candidate_hard_violations(
    candidate: TreatmentSearchCandidate,
    *,
    definition_coverage_m2: dict[str, float] | None = None,
    definition_protrusion_m: dict[str, float] | None = None,
    definition_cost: dict[str, float] | None = None,
    slot_positions_m: dict[tuple[str, str | None], tuple[float, float, float]]
    | None = None,
    spec: TreatmentSearchSpec | None = None,
) -> list[str]:
    """Evaluate hard constraints as infeasible evidence, never penalties.

    Physical inputs (coverage/protrusion/cost per definition, slot positions)
    are supplied by the caller from canonical authorities; anything not
    supplied simply is not checked — this function never invents values.
    """
    violations: list[str] = []
    if spec is not None:
        if candidate.spec_id != spec.spec_id:
            return ['candidate does not belong to this search spec']
        constraints = spec.hard_constraints
        allowed = set(spec.space.allowed_definition_ids)
        for assignment in candidate.assignments:
            if assignment.definition_id not in allowed:
                violations.append(
                    f'definition {assignment.definition_id} is outside the '
                    'declared search space'
                )
        if candidate.assignments:
            slots = {
                (item.slot.host_surface_id, item.slot.region_id)
                for item in candidate.assignments
            }
            declared = {
                (item.host_surface_id, item.region_id)
                for item in spec.space.candidate_slots
            }
            if not slots <= declared:
                violations.append(
                    'candidate uses slots outside the declared search space'
                )
            if len(candidate.assignments) > spec.space.max_treatment_count:
                violations.append('candidate exceeds max treatment count')
        for surface_id in spec.hard_constraints.excluded_surface_ids:
            if any(
                item.slot.host_surface_id == surface_id
                for item in candidate.assignments
            ):
                violations.append(
                    f'candidate places treatment on excluded surface {surface_id}'
                )
    else:
        constraints = TreatmentHardConstraints()

    coverage = definition_coverage_m2 or {}
    protrusion = definition_protrusion_m or {}
    cost = definition_cost or {}
    positions = slot_positions_m or {}

    total_area = sum(
        coverage.get(item.definition_id, 0.0) * item.quantity
        for item in candidate.assignments
    )
    if (
        constraints.max_coverage_m2 is not None
        and coverage
        and total_area > constraints.max_coverage_m2
    ):
        violations.append(
            f'coverage {total_area:.3f} m2 exceeds the '
            f'{constraints.max_coverage_m2} m2 limit'
        )
    if constraints.max_protrusion_m is not None and protrusion:
        worst = max(
            (
                protrusion.get(item.definition_id, 0.0)
                for item in candidate.assignments
            ),
            default=0.0,
        )
        if worst > constraints.max_protrusion_m:
            violations.append(
                f'protrusion {worst:.3f} m exceeds the '
                f'{constraints.max_protrusion_m} m limit'
            )
    if constraints.budget_ceiling is not None and cost:
        total_cost = sum(
            cost.get(item.definition_id, 0.0) * item.quantity
            for item in candidate.assignments
        )
        if total_cost > constraints.budget_ceiling:
            violations.append(
                f'cost {total_cost:.3f} exceeds the budget ceiling '
                f'{constraints.budget_ceiling}'
            )
    if constraints.min_spacing_m and len(candidate.assignments) > 1:
        pts = [
            positions.get((item.slot.host_surface_id, item.slot.region_id))
            for item in candidate.assignments
        ]
        if all(pt is not None for pt in pts):
            for i, a in enumerate(pts):
                for b in pts[i + 1 :]:
                    dist = (
                        (a[0] - b[0]) ** 2
                        + (a[1] - b[1]) ** 2
                        + (a[2] - b[2]) ** 2
                    ) ** 0.5
                    if dist < constraints.min_spacing_m:
                        violations.append(
                            f'slot spacing {dist:.3f} m is below the '
                            f'{constraints.min_spacing_m} m minimum'
                        )
                        return violations
    return violations
