from __future__ import annotations

from decimal import Decimal, InvalidOperation
from hashlib import sha256
from itertools import product
import json
from math import isfinite
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, model_validator

from .canonical_json import canonical_sha256
from .placement_constraints import PlacementEvaluationRequest, evaluate_constraint_set


SEARCH_SPACE_ALGORITHM_VERSION = 'search-space-grid-1'
SYSTEM_MAX_RAW_CANDIDATES = 50_000
# Single authority for the generation-page contract: one generate call never
# returns more than this many candidates, so full-set consumers paginate at
# min(MAX_SEARCH_PAGE_SIZE, candidate_limit) rather than assuming an unbounded
# page.
MAX_SEARCH_PAGE_SIZE = 500
_AXES = ('x', 'y', 'z')


class SearchGenerationCancelled(RuntimeError):
    pass


class GridAxis(BaseModel):
    entity_id: str = Field(min_length=1, max_length=100)
    axis: Literal['x', 'y', 'z']
    min_m: float
    max_m: float
    step_m: float = Field(gt=0)

    @model_validator(mode='after')
    def validate_range(self) -> 'GridAxis':
        values = (self.min_m, self.max_m, self.step_m)
        if not all(isfinite(value) for value in values):
            raise ValueError('Grid axis contains a non-finite numeric value')
        if self.max_m < self.min_m:
            raise ValueError('Grid axis max_m must be >= min_m')
        return self

class LinkedDerivation(BaseModel):
    constraint_id: str = Field(min_length=1, max_length=100)
    master_entity_id: str = Field(min_length=1, max_length=100)


class SearchSpecCreate(BaseModel):
    constraint_set_id: str = Field(min_length=1)
    name: str | None = Field(default=None, max_length=200)
    algorithm: Literal['deterministic_grid'] = 'deterministic_grid'
    axes: list[GridAxis] = Field(min_length=1)
    linked_derivations: list[LinkedDerivation] = Field(default_factory=list)
    candidate_limit: int = Field(default=10_000, ge=1, le=SYSTEM_MAX_RAW_CANDIDATES)

    @model_validator(mode='after')
    def validate_uniqueness(self) -> 'SearchSpecCreate':
        axis_keys = [(item.entity_id, item.axis) for item in self.axes]
        if len(axis_keys) != len(set(axis_keys)):
            raise ValueError('SearchSpec grid axes must be unique by entity_id + axis')
        link_ids = [item.constraint_id for item in self.linked_derivations]
        if len(link_ids) != len(set(link_ids)):
            raise ValueError('SearchSpec linked derivation constraint IDs must be unique')
        return self


class StoredSearchSpec(BaseModel):
    algorithm_version: Literal['search-space-grid-1']
    algorithm: Literal['deterministic_grid']
    context_id: str
    constraint_set_id: str
    constraint_set_spec_sha256: str
    axes: list[GridAxis] = Field(min_length=1)
    linked_derivations: list[LinkedDerivation] = Field(default_factory=list)
    candidate_limit: int = Field(ge=1, le=SYSTEM_MAX_RAW_CANDIDATES)

def _decimal(value: float) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f'Invalid decimal grid value: {value}') from exc


def grid_values(axis: GridAxis) -> list[float]:
    low, high, step = _decimal(axis.min_m), _decimal(axis.max_m), _decimal(axis.step_m)
    values: list[float] = []
    index = 0
    while True:
        value = low + step * index
        if value > high:
            break
        values.append(float(value))
        index += 1
        if index > SYSTEM_MAX_RAW_CANDIDATES:
            raise ValueError('Grid axis exceeds the system candidate limit')
    if not values:
        raise ValueError('Grid axis produced no values')
    return values


def _baseline_positions(context_payload: dict[str, Any]) -> dict[str, dict[str, float] | None]:
    point = context_payload['measurement_point']
    result: dict[str, dict[str, float] | None] = {str(point['point_id']): point.get('position')}
    for speaker in context_payload.get('speakers', []):
        entity_id = str(speaker['speaker_id'])
        if entity_id in result:
            raise ValueError(f'Duplicate Context entity_id: {entity_id}')
        result[entity_id] = speaker.get('position')
    return result

def _copy_baselines(baselines: dict[str, dict[str, float] | None]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for entity_id, position in baselines.items():
        if position is None:
            continue
        result[entity_id] = {
            axis: float(position[f'{axis}_m'])
            for axis in _AXES
        }
    return result


def _relation_axis(relation: str) -> str:
    if relation in ('mirror_x', 'equal_x', 'equal_delta_x'):
        return 'x'
    if relation in ('equal_y', 'equal_delta_y'):
        return 'y'
    if relation in ('equal_z', 'equal_delta_z'):
        return 'z'
    raise ValueError(f'Unsupported linked placement relation: {relation}')


def _linked_constraints(constraint_spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item['constraint_id']): item
        for item in constraint_spec.get('constraints', [])
        if item.get('kind') == 'linked_placement'
    }


def _derivation_plan(
    request: SearchSpecCreate,
    constraint_spec: dict[str, Any],
    baselines: dict[str, dict[str, float] | None],
) -> list[dict[str, Any]]:
    linked = _linked_constraints(constraint_spec)
    plan: list[dict[str, Any]] = []
    targets: set[tuple[str, str]] = set()
    grid_keys = {(item.entity_id, item.axis) for item in request.axes}
    ordered = sorted(request.linked_derivations, key=lambda item: (item.constraint_id, item.master_entity_id))
    for derivation in ordered:
        constraint = linked.get(derivation.constraint_id)
        if constraint is None:
            raise ValueError(f'Linked derivation references unknown linked constraint: {derivation.constraint_id}')
        a, b = str(constraint['entity_a']), str(constraint['entity_b'])
        if derivation.master_entity_id not in (a, b):
            raise ValueError(f'Linked derivation master is not part of constraint {derivation.constraint_id}')
        slave = b if derivation.master_entity_id == a else a
        axis = _relation_axis(str(constraint['relation']))
        target = (slave, axis)
        if target in targets:
            raise ValueError(f'Multiple linked derivations target {slave}.{axis}')
        if target in grid_keys:
            raise ValueError(f'{slave}.{axis} cannot be both a grid axis and linked derivation target')
        for entity_id in (derivation.master_entity_id, slave):
            if entity_id not in baselines:
                raise ValueError(f'Linked derivation references unknown entity_id: {entity_id}')
            if baselines[entity_id] is None:
                raise ValueError(f'Linked derivation requires a Context baseline position for {entity_id}')
        targets.add(target)
        plan.append({
            'constraint': constraint,
            'master': derivation.master_entity_id,
            'slave': slave,
            'axis': axis,
        })

    if any((item['master'], item['axis']) in targets for item in plan):
        raise ValueError('Chained or cyclic linked derivations are not supported in search-space-grid-1')
    return plan

def validate_search_spec(
    request: SearchSpecCreate,
    context_payload: dict[str, Any],
    *,
    context_id: str,
    constraint_set_id: str,
    constraint_set_spec: dict[str, Any],
    constraint_set_spec_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    baselines = _baseline_positions(context_payload)
    room = context_payload['room']
    bounds = {
        'x': (0.0, float(room['width_m'])),
        'y': (0.0, float(room['depth_m'])),
        'z': (0.0, float(room['height_m'])),
    }
    ordered_axes = sorted(request.axes, key=lambda item: (item.entity_id, _AXES.index(item.axis)))
    counts: list[dict[str, Any]] = []
    for axis in ordered_axes:
        if axis.entity_id not in baselines:
            raise ValueError(f'Grid axis references unknown entity_id: {axis.entity_id}')
        if baselines[axis.entity_id] is None:
            raise ValueError(f'Grid axis requires a Context baseline position for {axis.entity_id}')
        low, high = bounds[axis.axis]
        if axis.min_m < low or axis.max_m > high:
            raise ValueError(f'Grid axis {axis.entity_id}.{axis.axis} is outside room reference bounds {low}..{high}')
        values = grid_values(axis)
        counts.append({
            'entity_id': axis.entity_id,
            'axis': axis.axis,
            'count': len(values),
            'first_m': values[0],
            'last_m': values[-1],
        })
    plan = _derivation_plan(request, constraint_set_spec, baselines)
    raw_count = 1
    for item in counts:
        raw_count *= int(item['count'])
        if raw_count > SYSTEM_MAX_RAW_CANDIDATES:
            break
    if raw_count > request.candidate_limit:
        raise ValueError(f'Raw candidate estimate {raw_count} exceeds SearchSpec candidate_limit {request.candidate_limit}')

    ordered_links = sorted(
        request.linked_derivations,
        key=lambda item: (item.constraint_id, item.master_entity_id),
    )
    spec = {
        'algorithm_version': SEARCH_SPACE_ALGORITHM_VERSION,
        'algorithm': request.algorithm,
        'context_id': context_id,
        'constraint_set_id': constraint_set_id,
        'constraint_set_spec_sha256': constraint_set_spec_sha256,
        'axes': [item.model_dump(mode='json') for item in ordered_axes],
        'linked_derivations': [item.model_dump(mode='json') for item in ordered_links],
        'candidate_limit': request.candidate_limit,
    }
    estimate = {
        'raw_candidate_count': raw_count,
        'axis_counts': counts,
        'linked_derivation_count': len(plan),
    }
    return spec, estimate


def _set_axis(position: dict[str, float], axis: str, value: float) -> None:
    position[axis] = round(float(value), 12)

def _apply_derivations(
    resolved: dict[str, dict[str, float]],
    baselines: dict[str, dict[str, float] | None],
    plan: list[dict[str, Any]],
    context_payload: dict[str, Any],
) -> set[str]:
    touched: set[str] = set()
    for item in plan:
        constraint = item['constraint']
        master, slave, axis = item['master'], item['slave'], item['axis']
        relation = str(constraint['relation'])
        master_value = resolved[master][axis]
        if relation == 'mirror_x':
            mirror_axis = constraint.get('mirror_axis_x_m')
            if mirror_axis is None:
                mirror_axis = float(context_payload['room']['width_m']) / 2.0
            value = 2.0 * float(mirror_axis) - master_value
        elif relation.startswith('equal_delta_'):
            master_base = baselines[master]
            slave_base = baselines[slave]
            assert master_base is not None and slave_base is not None
            value = float(slave_base[f'{axis}_m']) + master_value - float(master_base[f'{axis}_m'])
        else:
            value = master_value
        _set_axis(resolved[slave], axis, value)
        touched.add(slave)
    return touched


def _canonical_sha(payload: Any) -> str:
    return canonical_sha256(payload)

def generate_search_space(
    context_payload: dict[str, Any],
    raw_search_spec: dict[str, Any],
    *,
    search_spec_sha256: str,
    constraint_set_spec: dict[str, Any],
    constraint_set_spec_sha256: str,
    offset: int = 0,
    limit: int = 100,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    if offset < 0:
        raise ValueError('offset must be >= 0')
    if limit < 1 or limit > MAX_SEARCH_PAGE_SIZE:
        raise ValueError('limit must be between 1 and 500')
    spec = StoredSearchSpec.model_validate(raw_search_spec)
    if spec.constraint_set_spec_sha256 != constraint_set_spec_sha256:
        raise ValueError('SearchSpec ConstraintSet hash no longer matches the referenced ConstraintSet')

    baselines = _baseline_positions(context_payload)
    request = SearchSpecCreate(
        constraint_set_id=spec.constraint_set_id,
        algorithm=spec.algorithm,
        axes=spec.axes,
        linked_derivations=spec.linked_derivations,
        candidate_limit=spec.candidate_limit,
    )
    plan = _derivation_plan(request, constraint_set_spec, baselines)
    ordered_axes = sorted(spec.axes, key=lambda item: (item.entity_id, _AXES.index(item.axis)))
    value_lists = [grid_values(item) for item in ordered_axes]
    raw_count = 1
    for values in value_lists:
        raw_count *= len(values)
    if raw_count > spec.candidate_limit:
        raise ValueError(f'Raw candidate estimate {raw_count} exceeds SearchSpec candidate_limit {spec.candidate_limit}')

    page: list[dict[str, Any]] = []
    candidate_ids: list[str] = []
    rejection_counts: dict[str, int] = {}
    seen_candidate_ids: set[str] = set()
    rejected_count = 0
    duplicate_count = 0
    if cancelled is not None and cancelled():
        raise SearchGenerationCancelled('search generation cancelled')
    for raw_index, combination in enumerate(product(*value_lists)):
        if cancelled is not None and cancelled():
            raise SearchGenerationCancelled('search generation cancelled')
        resolved = _copy_baselines(baselines)
        touched: set[str] = set()
        for axis_spec, value in zip(ordered_axes, combination, strict=True):
            _set_axis(resolved[axis_spec.entity_id], axis_spec.axis, value)
            touched.add(axis_spec.entity_id)
        touched.update(_apply_derivations(resolved, baselines, plan, context_payload))

        overrides = {
            entity_id: {f'{axis}_m': resolved[entity_id][axis] for axis in _AXES}
            for entity_id in sorted(touched)
        }
        evaluation = evaluate_constraint_set(
            context_payload,
            constraint_set_spec,
            PlacementEvaluationRequest.model_validate({'positions': overrides}),
        )
        if not evaluation['feasible']:
            rejected_count += 1
            for rejection in evaluation['rejections']:
                constraint_id = str(rejection['constraint_id'])
                rejection_counts[constraint_id] = rejection_counts.get(constraint_id, 0) + 1
            continue

        candidate_id = 'pc-' + _canonical_sha({
            'search_spec_sha256': search_spec_sha256,
            'positions': overrides,
        })[:20]
        if candidate_id in seen_candidate_ids:
            duplicate_count += 1
            continue
        seen_candidate_ids.add(candidate_id)
        feasible_index = len(candidate_ids)
        candidate_ids.append(candidate_id)
        if offset <= feasible_index < offset + limit:
            page.append({
                'candidate_id': candidate_id,
                'raw_index': raw_index,
                'feasible_index': feasible_index,
                'positions': overrides,
            })

    candidate_set_sha256 = _canonical_sha(candidate_ids)
    return {
        'classification': 'placement_search_space',
        'algorithm_version': SEARCH_SPACE_ALGORITHM_VERSION,
        'algorithm': spec.algorithm,
        'raw_candidate_count': raw_count,
        'feasible_candidate_count': len(candidate_ids),
        'rejected_candidate_count': rejected_count,
        'duplicate_candidate_count': duplicate_count,
        'candidate_limit': spec.candidate_limit,
        'rejection_counts': dict(sorted(rejection_counts.items())),
        'candidate_set_sha256': candidate_set_sha256,
        'offset': offset,
        'limit': limit,
        'returned_candidate_count': len(page),
        'candidates': page,
    }
