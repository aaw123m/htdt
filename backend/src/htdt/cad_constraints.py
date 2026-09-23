from __future__ import annotations

from math import hypot
from typing import Any, Iterable

from .cad_constraint_models import (
    CadAllowedRegionConstraint,
    CadConstraintEvaluation,
    CadConstraintResult,
    CadConstraintSet,
    CadExclusionRegionConstraint,
    CadPairDistanceConstraint,
    CadPlacementConstraint,
    CadWallClearanceConstraint,
)
from .cad_orientation_constraints import entity_horizontal_footprint
from .cad_scene import Position3, SceneDocument, room_vertices
from .placement_constraints import (
    ConstraintSetCreate,
    PlacementEvaluationRequest,
    evaluate_constraint_set,
    validate_constraint_set_for_context,
)


class CadConstraintAdapterError(ValueError):
    pass


_SYNTHETIC_POINT_PREFIX = '__cad_constraint_adapter_point__'


def _position_payload(position: Position3) -> dict[str, float]:
    return {
        'x_m': float(position.x_m),
        'y_m': float(position.y_m),
        'z_m': float(position.z_m),
    }


def wall_edge_maps(document: SceneDocument) -> tuple[dict[str, str], dict[str, str]]:
    room = document.room
    topology = document.wall_topology
    if room is None or topology is None:
        raise CadConstraintAdapterError('wall clearance requires room wall topology')

    vertices = room_vertices(room)
    room_edges = {
        f'{start.vertex_id}->{vertices[(index + 1) % len(vertices)].vertex_id}'
        for index, start in enumerate(vertices)
    }
    wall_to_edge: dict[str, str] = {}
    edge_to_wall: dict[str, str] = {}
    for wall in topology.walls:
        edge_id = f'{wall.from_vertex_id}->{wall.to_vertex_id}'
        if edge_id not in room_edges:
            raise CadConstraintAdapterError(
                f'wall {wall.wall_id} direction/endpoints do not match current room boundary: {edge_id}'
            )
        if edge_id in edge_to_wall:
            raise CadConstraintAdapterError(f'duplicate legacy wall edge mapping: {edge_id}')
        wall_to_edge[wall.wall_id] = edge_id
        edge_to_wall[edge_id] = wall.wall_id
    return wall_to_edge, edge_to_wall


def _constraint_entity_ids(constraint: CadPlacementConstraint) -> tuple[str, ...]:
    if isinstance(constraint, (CadAllowedRegionConstraint, CadExclusionRegionConstraint, CadWallClearanceConstraint)):
        return tuple(constraint.entity_ids)
    if isinstance(constraint, CadPairDistanceConstraint):
        return (constraint.entity_a, constraint.entity_b)
    raise TypeError(type(constraint).__name__)


def _relevant_entity_ids(constraint_set: CadConstraintSet) -> set[str]:
    result: set[str] = set()
    for constraint in constraint_set.constraints:
        result.update(_constraint_entity_ids(constraint))
    return result


def _synthetic_point_id(document: SceneDocument) -> str:
    known = {entity.entity_id for entity in document.entities}
    candidate = _SYNTHETIC_POINT_PREFIX
    suffix = 1
    while candidate in known:
        candidate = f'{_SYNTHETIC_POINT_PREFIX}{suffix}'
        suffix += 1
    return candidate


def scene_to_g10_context(document: SceneDocument) -> dict[str, Any]:
    room = document.room
    if room is None:
        raise CadConstraintAdapterError('placement constraints require a room')

    vertices = room_vertices(room)
    measurement_points = [entity for entity in document.entities if entity.kind == 'measurement_point']
    primary_point = measurement_points[0] if measurement_points else None
    primary_point_id = primary_point.entity_id if primary_point is not None else _synthetic_point_id(document)
    primary_position = _position_payload(primary_point.position) if primary_point is not None else None

    carriers: list[dict[str, Any]] = []
    for entity in document.entities:
        if primary_point is not None and entity.entity_id == primary_point.entity_id:
            continue
        carriers.append({
            'speaker_id': entity.entity_id,
            'role': f'cad_adapter:{entity.kind}',
            'position': _position_payload(entity.position),
        })

    return {
        'room': {
            'width_m': float(room.width_m),
            'depth_m': float(room.depth_m),
            'height_m': float(room.height_m),
            'geometry_kind': 'polygon_prism',
            'footprint_vertices': [
                {'vertex_id': vertex.vertex_id, 'x_m': float(vertex.x_m), 'y_m': float(vertex.y_m)}
                for vertex in vertices
            ],
        },
        'speakers': carriers,
        'measurement_point': {
            'point_id': primary_point_id,
            'label': 'CAD adapter reference',
            'position': primary_position,
        },
        'avr': {'manufacturer': 'HTDT', 'model': 'CAD constraint adapter'},
    }


def _g10_constraint_payload(
    constraint: CadPlacementConstraint,
    wall_to_edge: dict[str, str] | None,
) -> dict[str, Any]:
    if isinstance(constraint, CadAllowedRegionConstraint):
        return {
            'constraint_id': constraint.constraint_id,
            'kind': 'allowed_region',
            'entity_ids': list(constraint.entity_ids),
            'region': {'vertices': [item.model_dump(mode='json') for item in constraint.vertices]},
        }
    if isinstance(constraint, CadExclusionRegionConstraint):
        return {
            'constraint_id': constraint.constraint_id,
            'kind': 'exclusion_region',
            'entity_ids': list(constraint.entity_ids),
            'region': {'vertices': [item.model_dump(mode='json') for item in constraint.vertices]},
        }
    if isinstance(constraint, CadWallClearanceConstraint):
        if wall_to_edge is None:
            raise CadConstraintAdapterError('wall clearance requires wall topology')
        edge_id = wall_to_edge.get(constraint.wall_id)
        if edge_id is None:
            raise CadConstraintAdapterError(
                f'constraint {constraint.constraint_id} references unknown wall_id: {constraint.wall_id}'
            )
        return {
            'constraint_id': constraint.constraint_id,
            'kind': 'wall_clearance',
            'entity_ids': list(constraint.entity_ids),
            'edge_id': edge_id,
            'min_m': constraint.min_m,
            'max_m': constraint.max_m,
        }
    if isinstance(constraint, CadPairDistanceConstraint):
        return {
            'constraint_id': constraint.constraint_id,
            'kind': 'pair_distance',
            'entity_a': constraint.entity_a,
            'entity_b': constraint.entity_b,
            'min_m': constraint.min_m,
            'max_m': constraint.max_m,
            'distance_mode': constraint.distance_mode,
            'distance_reference': constraint.distance_reference,
        }
    raise TypeError(type(constraint).__name__)


def _entity_profile(document: SceneDocument, entity_id: str) -> dict[str, Any] | None:
    entity = document.entity(entity_id)
    if entity.size_m is None:
        return None
    radius = hypot(float(entity.size_m.x_m) * 0.5, float(entity.size_m.y_m) * 0.5)
    footprint = entity_horizontal_footprint(entity)
    vertices = [
        {
            'x_m': float(x) - float(entity.position.x_m),
            'y_m': float(y) - float(entity.position.y_m),
        }
        for x, y in list(footprint.exterior.coords)[:-1]
    ]
    return {
        'entity_id': entity.entity_id,
        # Keep the legacy radius for backwards-compatible diagnostics/fallback.
        'footprint_radius_m': radius,
        'safety_margin_m': 0.0,
        'footprint_vertices_xy_m': vertices,
    }


def build_g10_constraint_request(
    document: SceneDocument,
    constraint_set: CadConstraintSet,
    *,
    additional_entity_ids: Iterable[str] = (),
) -> ConstraintSetCreate:
    if constraint_set.document_id != document.document_id:
        raise CadConstraintAdapterError(
            f'constraint workspace document_id {constraint_set.document_id} does not match scene {document.document_id}'
        )
    known_entities = {entity.entity_id for entity in document.entities}
    relevant = _relevant_entity_ids(constraint_set) | {str(entity_id) for entity_id in additional_entity_ids}
    unknown = relevant - known_entities
    if unknown:
        raise CadConstraintAdapterError('constraints/search axes reference unknown CAD entities: ' + ', '.join(sorted(unknown)))

    needs_wall_map = any(isinstance(item, CadWallClearanceConstraint) for item in constraint_set.constraints)
    wall_to_edge = wall_edge_maps(document)[0] if needs_wall_map else None
    profiles = [
        profile
        for entity_id in sorted(relevant)
        if (profile := _entity_profile(document, entity_id)) is not None
    ]
    payload = {
        'context_id': f'cad:{document.document_id}',
        'name': 'Native CAD placement constraints',
        'entity_profiles': profiles,
        'constraints': [
            _g10_constraint_payload(item, wall_to_edge)
            for item in constraint_set.constraints
        ],
    }
    return ConstraintSetCreate.model_validate(payload)


def _result_metadata(
    constraint_set: CadConstraintSet,
    constraint_id: str,
) -> tuple[str, str | None, str | None]:
    if constraint_id.startswith('__room_boundary__:'):
        return ('部屋境界', None, None)
    native = constraint_set.constraint(constraint_id)
    if isinstance(native, CadWallClearanceConstraint):
        return (native.name, native.wall_id, None)
    if isinstance(native, CadAllowedRegionConstraint):
        return (native.name, None, 'allowed')
    if isinstance(native, CadExclusionRegionConstraint):
        return (native.name, None, native.region_role)
    return (native.name, None, None)


def _reason(kind: str, passed: bool, region_role: str | None) -> tuple[str, str]:
    if passed:
        return (f'{kind}.passed', '制約を満たしています')
    if kind == 'room_boundary':
        return ('room_boundary.outside', '部屋の範囲外です')
    if kind == 'allowed_region':
        return ('allowed_region.outside', '許可領域の外です')
    if kind == 'exclusion_region':
        if region_role == 'walkway':
            return ('exclusion_region.walkway_intersection', '通路に干渉しています')
        return ('exclusion_region.intersection', '除外領域に干渉しています')
    if kind == 'wall_clearance':
        return ('wall_clearance.out_of_range', '壁との離隔が必要範囲を満たしていません')
    if kind == 'pair_distance':
        return ('pair_distance.out_of_range', '物体間距離が必要範囲を満たしていません')
    return (f'{kind}.failed', '配置制約を満たしていません')


def _scalar_distance(kind: str, actual: dict[str, Any], required: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    if kind == 'wall_clearance':
        return (
            float(actual['clearance_m']) if actual.get('clearance_m') is not None else None,
            float(required['min_m']) if required.get('min_m') is not None else None,
            float(required['max_m']) if required.get('max_m') is not None else None,
        )
    if kind == 'pair_distance':
        return (
            float(actual['distance_m']) if actual.get('distance_m') is not None else None,
            float(required['min_m']) if required.get('min_m') is not None else None,
            float(required['max_m']) if required.get('max_m') is not None else None,
        )
    return (None, None, None)


def evaluate_cad_constraints(
    document: SceneDocument,
    constraint_set: CadConstraintSet,
    *,
    position_overrides: dict[str, Position3] | None = None,
) -> CadConstraintEvaluation:
    if constraint_set.document_id != document.document_id:
        raise CadConstraintAdapterError(
            f'constraint workspace document_id {constraint_set.document_id} does not match scene {document.document_id}'
        )
    if not constraint_set.constraints:
        return CadConstraintEvaluation(constraints_satisfied=True, results=())

    context = scene_to_g10_context(document)
    request = build_g10_constraint_request(document, constraint_set)
    stored_spec = validate_constraint_set_for_context(request, context)
    overrides = {
        entity_id: _position_payload(position)
        for entity_id, position in (position_overrides or {}).items()
    }
    raw = evaluate_constraint_set(
        context,
        stored_spec,
        PlacementEvaluationRequest.model_validate({'positions': overrides}),
    )

    results: list[CadConstraintResult] = []
    for observation in raw['observations']:
        constraint_id = str(observation['constraint_id'])
        passed = bool(observation['passed'])
        if constraint_id.startswith('__room_boundary__:') and passed:
            continue
        kind = str(observation['kind'])
        entity_ids = tuple(str(item) for item in observation.get('entity_ids', []))
        name, wall_id, region_role = _result_metadata(constraint_set, constraint_id)
        reason_code, reason_ja = _reason(kind, passed, region_role)
        actual = dict(observation.get('actual', {}))
        required = dict(observation.get('required', {}))
        actual_m, required_min_m, required_max_m = _scalar_distance(kind, actual, required)
        result_id = ':'.join((constraint_id, *entity_ids))
        results.append(CadConstraintResult(
            result_id=result_id,
            constraint_id=constraint_id,
            kind=kind,
            name=name,
            entity_ids=entity_ids,
            wall_id=wall_id,
            region_role=region_role,
            passed=passed,
            reason_code=reason_code,
            reason_ja=reason_ja,
            actual_m=actual_m,
            required_min_m=required_min_m,
            required_max_m=required_max_m,
            raw_actual=actual,
            raw_required=required,
        ))

    return CadConstraintEvaluation(
        constraints_satisfied=bool(raw['feasible']),
        results=tuple(results),
    )
