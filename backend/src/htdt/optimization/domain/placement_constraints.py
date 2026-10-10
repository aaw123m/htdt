from __future__ import annotations

from dataclasses import dataclass
from math import isfinite, sqrt
from typing import Any, Annotated, Literal

from pydantic import BaseModel, Field, model_validator
from shapely.affinity import translate
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from ...geometry import polygon_from_vertices, room_geometry_payload


PLACEMENT_CONSTRAINT_ENGINE_VERSION = 'placement-constraints-1'
_EPS = 1e-9


class ConstraintPoint2D(BaseModel):
    x_m: float
    y_m: float


class CandidatePoint3D(BaseModel):
    x_m: float
    y_m: float
    z_m: float

    @model_validator(mode='after')
    def finite_coordinates(self) -> 'CandidatePoint3D':
        if not all(isfinite(value) for value in (self.x_m, self.y_m, self.z_m)):
            raise ValueError('Candidate position contains a non-finite coordinate')
        return self


class ConstraintRegion(BaseModel):
    vertices: list[ConstraintPoint2D] | None = None
    polygons: list[list[ConstraintPoint2D]] | None = None

    @model_validator(mode='after')
    def valid_polygon(self) -> 'ConstraintRegion':
        if (self.vertices is None) == (self.polygons is None):
            raise ValueError('ConstraintRegion requires exactly one of vertices or polygons')
        groups = [self.vertices] if self.vertices is not None else self.polygons or []
        if not groups:
            raise ValueError('ConstraintRegion requires at least one polygon')
        for group in groups:
            if group is None or len(group) < 3:
                raise ValueError('ConstraintRegion polygon must have at least three vertices')
            polygon_from_vertices([(item.x_m, item.y_m) for item in group])
        return self


class EntityProfile(BaseModel):
    entity_id: str = Field(min_length=1, max_length=100)
    footprint_radius_m: float = Field(default=0.0, ge=0)
    safety_margin_m: float = Field(default=0.0, ge=0)
    footprint_vertices_xy_m: list[ConstraintPoint2D] | None = None
    # R120B: world-Z half-extent of the entity's oriented bounding envelope
    # about its position (position is the envelope centre). None means the
    # entity declares no Z extent — the room-prism check falls back to the
    # centre point and entity-collision checks skip the entity.
    z_extent_m: float | None = Field(default=None, ge=0)

    @model_validator(mode='after')
    def valid_footprint(self) -> 'EntityProfile':
        vertices = self.footprint_vertices_xy_m
        if vertices is not None:
            if len(vertices) < 3:
                raise ValueError('entity footprint requires at least three vertices')
            polygon_from_vertices([(item.x_m, item.y_m) for item in vertices])
        if self.z_extent_m is not None and not isfinite(self.z_extent_m):
            raise ValueError('entity z extent must be finite')
        return self

    @property
    def effective_radius_m(self) -> float:
        return self.footprint_radius_m + self.safety_margin_m


class AllowedRegionConstraint(BaseModel):
    constraint_id: str = Field(min_length=1, max_length=100)
    kind: Literal['allowed_region']
    entity_ids: list[str] = Field(min_length=1)
    region: ConstraintRegion


class ExclusionRegionConstraint(BaseModel):
    constraint_id: str = Field(min_length=1, max_length=100)
    kind: Literal['exclusion_region']
    entity_ids: list[str] = Field(min_length=1)
    region: ConstraintRegion


class WallClearanceConstraint(BaseModel):
    constraint_id: str = Field(min_length=1, max_length=100)
    kind: Literal['wall_clearance']
    entity_ids: list[str] = Field(min_length=1)
    edge_id: str = Field(min_length=1, max_length=220)
    min_m: float | None = Field(default=None, ge=0)
    max_m: float | None = Field(default=None, ge=0)

    @model_validator(mode='after')
    def valid_range(self) -> 'WallClearanceConstraint':
        if self.min_m is None and self.max_m is None:
            raise ValueError('wall_clearance requires min_m or max_m')
        if self.min_m is not None and self.max_m is not None and self.max_m < self.min_m:
            raise ValueError('wall_clearance max_m must be >= min_m')
        return self


class AxisConstraint(BaseModel):
    constraint_id: str = Field(min_length=1, max_length=100)
    kind: Literal['axis_range']
    entity_id: str = Field(min_length=1, max_length=100)
    axis: Literal['x', 'y', 'z']
    min_m: float | None = None
    max_m: float | None = None
    fixed_m: float | None = None
    tolerance_m: float = Field(default=1e-6, ge=0)

    @model_validator(mode='after')
    def valid_axis(self) -> 'AxisConstraint':
        if self.fixed_m is None and self.min_m is None and self.max_m is None:
            raise ValueError('axis_range requires fixed_m, min_m, or max_m')
        if self.min_m is not None and self.max_m is not None and self.max_m < self.min_m:
            raise ValueError('axis_range max_m must be >= min_m')
        return self


class MovementBudgetConstraint(BaseModel):
    constraint_id: str = Field(min_length=1, max_length=100)
    kind: Literal['movement_budget']
    entity_id: str = Field(min_length=1, max_length=100)
    max_distance_m: float = Field(ge=0)
    distance_mode: Literal['3d', 'horizontal_xy'] = '3d'


class PairDistanceConstraint(BaseModel):
    constraint_id: str = Field(min_length=1, max_length=100)
    kind: Literal['pair_distance']
    entity_a: str = Field(min_length=1, max_length=100)
    entity_b: str = Field(min_length=1, max_length=100)
    min_m: float | None = Field(default=None, ge=0)
    max_m: float | None = Field(default=None, ge=0)
    distance_mode: Literal['3d', 'horizontal_xy'] = '3d'
    distance_reference: Literal['center', 'envelope_clearance'] = 'center'

    @model_validator(mode='after')
    def valid_pair(self) -> 'PairDistanceConstraint':
        if self.entity_a == self.entity_b:
            raise ValueError('pair_distance requires two different entities')
        if self.min_m is None and self.max_m is None:
            raise ValueError('pair_distance requires min_m or max_m')
        if self.min_m is not None and self.max_m is not None and self.max_m < self.min_m:
            raise ValueError('pair_distance max_m must be >= min_m')
        if self.distance_reference == 'envelope_clearance' and self.distance_mode != 'horizontal_xy':
            raise ValueError('envelope_clearance requires distance_mode=horizontal_xy because footprint radii are horizontal')
        return self


class EntityCollisionConstraint(BaseModel):
    """Declared 3D entity-entity collision prohibition (R120B).

    Requires both entities to declare an XY envelope AND a Z extent in the
    constraint set's entity profiles; when either cannot be established the
    constraint fails closed rather than treating overlap as unknown.
    """
    constraint_id: str = Field(min_length=1, max_length=100)
    kind: Literal['entity_collision']
    entity_a: str = Field(min_length=1, max_length=100)
    entity_b: str = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def different_entities(self) -> 'EntityCollisionConstraint':
        if self.entity_a == self.entity_b:
            raise ValueError('entity_collision requires two different entities')
        return self


class LinkedPlacementConstraint(BaseModel):
    constraint_id: str = Field(min_length=1, max_length=100)
    kind: Literal['linked_placement']
    entity_a: str = Field(min_length=1, max_length=100)
    entity_b: str = Field(min_length=1, max_length=100)
    relation: Literal['mirror_x', 'equal_x', 'equal_y', 'equal_z', 'equal_delta_x', 'equal_delta_y', 'equal_delta_z']
    mirror_axis_x_m: float | None = None
    tolerance_m: float = Field(default=1e-6, ge=0)

    @model_validator(mode='after')
    def different_entities(self) -> 'LinkedPlacementConstraint':
        if self.entity_a == self.entity_b:
            raise ValueError('linked_placement requires two different entities')
        if self.relation != 'mirror_x' and self.mirror_axis_x_m is not None:
            raise ValueError('mirror_axis_x_m is only valid for relation=mirror_x')
        return self


PlacementConstraint = Annotated[
    AllowedRegionConstraint
    | ExclusionRegionConstraint
    | WallClearanceConstraint
    | AxisConstraint
    | MovementBudgetConstraint
    | PairDistanceConstraint
    | EntityCollisionConstraint
    | LinkedPlacementConstraint,
    Field(discriminator='kind'),
]


class ConstraintSetCreate(BaseModel):
    context_id: str = Field(min_length=1)
    name: str | None = Field(default=None, max_length=200)
    entity_profiles: list[EntityProfile] = Field(default_factory=list)
    constraints: list[PlacementConstraint] = Field(default_factory=list)

    @model_validator(mode='after')
    def unique_ids(self) -> 'ConstraintSetCreate':
        profile_ids = [item.entity_id for item in self.entity_profiles]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError('entity_profiles entity_id values must be unique')
        constraint_ids = [item.constraint_id for item in self.constraints]
        if len(constraint_ids) != len(set(constraint_ids)):
            raise ValueError('constraint_id values must be unique')
        for item in self.constraints:
            if isinstance(item, (AllowedRegionConstraint, ExclusionRegionConstraint, WallClearanceConstraint)):
                if len(item.entity_ids) != len(set(item.entity_ids)):
                    raise ValueError(f'Constraint {item.constraint_id} entity_ids must be unique')
        def check_finite(value: Any) -> None:
            if isinstance(value, float) and not isfinite(value):
                raise ValueError('ConstraintSet contains a non-finite numeric value')
            if isinstance(value, dict):
                for child in value.values(): check_finite(child)
            elif isinstance(value, list):
                for child in value: check_finite(child)
        check_finite(self.model_dump(mode='python'))
        return self


class PlacementEvaluationRequest(BaseModel):
    positions: dict[str, CandidatePoint3D] = Field(default_factory=dict)


def _entity_baselines(context_payload: dict[str, Any]) -> dict[str, dict[str, float] | None]:
    measurement_point = context_payload['measurement_point']
    result: dict[str, dict[str, float] | None] = {
        str(measurement_point['point_id']): measurement_point.get('position')
    }
    for speaker in context_payload.get('speakers', []):
        entity_id = str(speaker['speaker_id'])
        if entity_id in result:
            raise ValueError(f'Duplicate Context entity_id: {entity_id}')
        result[entity_id] = speaker.get('position')
    return result


def _room_polygon_and_edges(context_payload: dict[str, Any]) -> tuple[Polygon, dict[str, LineString], dict[str, Any]]:
    geometry = room_geometry_payload(context_payload['room'])
    if not geometry['exact_footprint_available']:
        raise ValueError('Placement constraints require an exact room footprint; reference_box is insufficient')
    vertices = geometry['footprint_vertices']
    polygon = polygon_from_vertices([(float(item['x_m']), float(item['y_m'])) for item in vertices])
    edges: dict[str, LineString] = {}
    for index, start in enumerate(vertices):
        end = vertices[(index + 1) % len(vertices)]
        edge_id = f"{start['vertex_id']}->{end['vertex_id']}"
        edges[edge_id] = LineString(((float(start['x_m']), float(start['y_m'])), (float(end['x_m']), float(end['y_m']))))
    return polygon, edges, geometry


def _constraint_entity_ids(constraint: PlacementConstraint) -> tuple[str, ...]:
    if isinstance(constraint, (AllowedRegionConstraint, ExclusionRegionConstraint, WallClearanceConstraint)):
        return tuple(constraint.entity_ids)
    if isinstance(constraint, (AxisConstraint, MovementBudgetConstraint)):
        return (constraint.entity_id,)
    if isinstance(constraint, (PairDistanceConstraint, EntityCollisionConstraint, LinkedPlacementConstraint)):
        return (constraint.entity_a, constraint.entity_b)
    raise TypeError(type(constraint).__name__)


def validate_constraint_set_for_context(request: ConstraintSetCreate, context_payload: dict[str, Any]) -> dict[str, Any]:
    room_polygon, edges, geometry = _room_polygon_and_edges(context_payload)
    baselines = _entity_baselines(context_payload)
    known_ids = set(baselines)
    for profile in request.entity_profiles:
        if profile.entity_id not in known_ids:
            raise ValueError(f'Unknown entity_id in entity_profiles: {profile.entity_id}')
    for constraint in request.constraints:
        for entity_id in _constraint_entity_ids(constraint):
            if entity_id not in known_ids:
                raise ValueError(f'Constraint {constraint.constraint_id} references unknown entity_id: {entity_id}')
        if isinstance(constraint, WallClearanceConstraint) and constraint.edge_id not in edges:
            raise ValueError(f'Constraint {constraint.constraint_id} references unknown wall edge: {constraint.edge_id}')
        if isinstance(constraint, LinkedPlacementConstraint) and constraint.relation == 'mirror_x' and constraint.mirror_axis_x_m is not None:
            width = float(context_payload['room']['width_m'])
            if not (0.0 <= constraint.mirror_axis_x_m <= width):
                raise ValueError(f'Constraint {constraint.constraint_id} mirror axis is outside the room reference width')
        if isinstance(constraint, (AllowedRegionConstraint, ExclusionRegionConstraint)):
            region = _region_geometry(constraint.region)
            if not room_polygon.covers(region):
                raise ValueError(f'Constraint {constraint.constraint_id} region must be fully inside the exact room footprint')
        if isinstance(constraint, EntityCollisionConstraint):
            profile_by_id = {
                item.entity_id: item for item in request.entity_profiles
            }
            for entity_id in (constraint.entity_a, constraint.entity_b):
                profile = profile_by_id.get(entity_id)
                if profile is None or profile.z_extent_m is None:
                    raise ValueError(
                        f'Constraint {constraint.constraint_id} requires a '
                        f'declared XY envelope and Z extent for {entity_id}'
                    )
        if isinstance(constraint, MovementBudgetConstraint) and baselines[constraint.entity_id] is None:
            raise ValueError(f'Constraint {constraint.constraint_id} requires a baseline position for {constraint.entity_id}')
        if isinstance(constraint, LinkedPlacementConstraint) and constraint.relation.startswith('equal_delta_'):
            for entity_id in (constraint.entity_a, constraint.entity_b):
                if baselines[entity_id] is None:
                    raise ValueError(f'Constraint {constraint.constraint_id} requires a baseline position for {entity_id}')
    return {
        'engine_version': PLACEMENT_CONSTRAINT_ENGINE_VERSION,
        'geometry_version': geometry['geometry_version'],
        'entity_profiles': [item.model_dump(mode='json') for item in request.entity_profiles],
        'constraints': [item.model_dump(mode='json') for item in request.constraints],
    }


def _position_tuple(position: dict[str, Any]) -> tuple[float, float, float]:
    return float(position['x_m']), float(position['y_m']), float(position['z_m'])


def _distance(a: tuple[float, float, float], b: tuple[float, float, float], mode: str) -> float:
    dimensions = 2 if mode == 'horizontal_xy' else 3
    return sqrt(sum((a[index] - b[index]) ** 2 for index in range(dimensions)))


def _region_geometry(region: ConstraintRegion):
    groups = [region.vertices] if region.vertices is not None else region.polygons or []
    polygons = [polygon_from_vertices([(item.x_m, item.y_m) for item in group or []]) for group in groups]
    return polygons[0] if len(polygons) == 1 else unary_union(polygons)


def _entity_envelope(
    point: Point,
    profile: EntityProfile | None,
) -> BaseGeometry:
    if profile is None:
        return point
    vertices = profile.footprint_vertices_xy_m
    if vertices is not None:
        local = polygon_from_vertices(
            [(float(item.x_m), float(item.y_m)) for item in vertices]
        )
        envelope: BaseGeometry = translate(
            local,
            xoff=float(point.x),
            yoff=float(point.y),
        )
        if profile.safety_margin_m > _EPS:
            envelope = envelope.buffer(float(profile.safety_margin_m))
        return envelope
    radius_m = profile.effective_radius_m
    return point if radius_m <= _EPS else point.buffer(radius_m)


def _envelope_xy_collision(a: BaseGeometry, b: BaseGeometry) -> bool:
    """Positive-intersection XY collision between two entity envelopes.

    Two envelopes collide when their interiors overlap (positive-area
    intersection) or when a point envelope lands strictly inside another —
    shared boundaries and vertex touches are adjacency, not collision.
    """
    if a.is_empty or b.is_empty:
        return False
    intersection = a.intersection(b)
    if intersection.is_empty:
        return False
    if getattr(intersection, 'area', 0.0) > _EPS:
        return True
    if isinstance(a, Point):
        if isinstance(b, Point):
            return a.equals_exact(b, _EPS)
        return b.contains(a)
    if isinstance(b, Point):
        return a.contains(b)
    return False


def _profile_observation(profile: EntityProfile | None) -> dict[str, Any]:
    if profile is None:
        return {
            'footprint_mode': 'point',
            'effective_radius_m': 0.0,
        }
    return {
        'footprint_mode': (
            'oriented_polygon'
            if profile.footprint_vertices_xy_m is not None
            else 'legacy_radius'
        ),
        'effective_radius_m': profile.effective_radius_m,
        'z_extent_m': profile.z_extent_m,
    }


def _rejection(constraint_id: str, kind: str, entity_ids: list[str], message: str, **details: Any) -> dict[str, Any]:
    return {
        'constraint_id': constraint_id,
        'kind': kind,
        'entity_ids': entity_ids,
        'message': message,
        'details': details,
    }


def _resolved_positions(baselines: dict[str, dict[str, float] | None], overrides: dict[str, CandidatePoint3D]) -> tuple[dict[str, dict[str, float] | None], set[str]]:
    positions = dict(baselines)
    unknown = set(overrides) - set(positions)
    if unknown:
        raise ValueError('Unknown candidate entity_id values: ' + ', '.join(sorted(unknown)))
    for entity_id, point in overrides.items():
        positions[entity_id] = point.model_dump(mode='json')
    return positions, set(overrides)


class StoredConstraintSetSpec(BaseModel):
    engine_version: Literal['placement-constraints-1']
    geometry_version: str
    entity_profiles: list[EntityProfile] = Field(default_factory=list)
    constraints: list[PlacementConstraint] = Field(default_factory=list)


@dataclass(frozen=True)
class PreparedConstraintContext:
    """Context-derived evaluation state invariant across candidate requests.

    Room geometry, wall edges and entity baselines depend only on the Context
    payload, so batch callers (search enumeration, topology sweeps) can prepare
    them once instead of rebuilding shapely structures per candidate.
    """

    room_polygon: Polygon
    edges: dict[str, LineString]
    geometry: dict[str, Any]
    baselines: dict[str, dict[str, float] | None]
    room_height: float
    room_width: float


@dataclass(frozen=True)
class PreparedConstraintEvaluation:
    """Context+spec state hoisted out of per-candidate evaluation.

    Spec validation, entity profiles and region geometries are invariant when a
    batch enumerates candidates against one pinned constraint spec; preparing
    them once keeps per-candidate work to position resolution and geometry
    checks only.
    """

    context: PreparedConstraintContext
    spec: StoredConstraintSetSpec
    profiles: dict[str, EntityProfile]
    base_relevant_ids: frozenset[str]
    region_geometries: tuple[BaseGeometry | None, ...]


def prepare_constraint_context(
    context_payload: dict[str, Any],
) -> PreparedConstraintContext:
    """Prepare the Context-invariant half of constraint evaluation."""
    room_polygon, edges, geometry = _room_polygon_and_edges(context_payload)
    return PreparedConstraintContext(
        room_polygon=room_polygon,
        edges=edges,
        geometry=geometry,
        baselines=_entity_baselines(context_payload),
        room_height=float(context_payload['room']['height_m']),
        room_width=float(context_payload['room']['width_m']),
    )


def prepare_constraint_spec(
    context: PreparedConstraintContext,
    raw_spec: dict[str, Any],
) -> PreparedConstraintEvaluation:
    """Validate a stored constraint spec once against a prepared context."""
    spec = StoredConstraintSetSpec.model_validate(raw_spec)
    if spec.geometry_version != context.geometry['geometry_version']:
        raise ValueError(
            f"ConstraintSet geometry version {spec.geometry_version} does not match current engine geometry version {context.geometry['geometry_version']}"
        )
    relevant_ids: set[str] = set()
    region_geometries: list[BaseGeometry | None] = []
    for constraint in spec.constraints:
        relevant_ids.update(_constraint_entity_ids(constraint))
        if isinstance(constraint, (AllowedRegionConstraint, ExclusionRegionConstraint)):
            region_geometries.append(_region_geometry(constraint.region))
        else:
            region_geometries.append(None)
    profiles = {item.entity_id: item for item in spec.entity_profiles}
    relevant_ids.update(profiles)
    return PreparedConstraintEvaluation(
        context=context,
        spec=spec,
        profiles=profiles,
        base_relevant_ids=frozenset(relevant_ids),
        region_geometries=tuple(region_geometries),
    )


def prepare_constraint_evaluation(
    context_payload: dict[str, Any],
    raw_spec: dict[str, Any],
) -> PreparedConstraintEvaluation:
    """Prepare one Context+spec pair for repeated candidate evaluation."""
    return prepare_constraint_spec(
        prepare_constraint_context(context_payload),
        raw_spec,
    )


def evaluate_prepared_constraint_set(
    prepared: PreparedConstraintEvaluation,
    request: PlacementEvaluationRequest,
) -> dict[str, Any]:
    context = prepared.context
    spec = prepared.spec
    room_polygon = context.room_polygon
    edges = context.edges
    geometry = context.geometry
    baselines = context.baselines
    profiles = prepared.profiles
    resolved, overridden = _resolved_positions(baselines, request.positions)
    rejections: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []

    relevant_ids = prepared.base_relevant_ids | overridden
    room_height = context.room_height
    for entity_id in sorted(relevant_ids):
        position = resolved.get(entity_id)
        if position is None:
            continue
        x_m, y_m, z_m = _position_tuple(position)
        profile = profiles.get(entity_id)
        point = Point(x_m, y_m)
        envelope = _entity_envelope(point, profile)
        xy_inside = bool(room_polygon.covers(envelope))
        # R120B: the floor side keeps the legacy origin-point semantics
        # (physical entities anchor to the floor by their origin), while
        # the ceiling side upgrades to the full declared envelope — a
        # placement whose XY envelope fits but whose 3D envelope exceeds
        # the room height is still rejected. Entities without a declared
        # Z extent keep the legacy centre-point check on both sides.
        z_extent = None if profile is None else profile.z_extent_m
        if z_extent is None:
            z_bottom = None
            z_top = None
            z_inside = -_EPS <= z_m <= room_height + _EPS
        else:
            z_bottom = z_m - float(z_extent)
            z_top = z_m + float(z_extent)
            z_inside = z_m >= -_EPS and z_top <= room_height + _EPS
        actual: dict[str, Any] = {
            'xy_inside': xy_inside,
            'z_m': z_m,
            **_profile_observation(profile),
        }
        if z_extent is not None:
            actual['z_bottom_m'] = z_bottom
            actual['z_top_m'] = z_top
        observations.append({
            'constraint_id': f'__room_boundary__:{entity_id}',
            'kind': 'room_boundary',
            'entity_ids': [entity_id],
            'actual': actual,
            'required': {'xy_inside': True, 'z_range_m': [0.0, room_height]},
            'passed': xy_inside and z_inside,
        })
        if not xy_inside or not z_inside:
            rejection_details: dict[str, Any] = {
                'xy_inside': xy_inside,
                'z_m': z_m,
                'z_range_m': [0.0, room_height],
                **_profile_observation(profile),
            }
            if z_extent is not None:
                rejection_details['z_bottom_m'] = z_bottom
                rejection_details['z_top_m'] = z_top
            rejections.append(_rejection(
                f'__room_boundary__:{entity_id}', 'room_boundary', [entity_id],
                'Entity envelope is outside the exact room prism',
                **rejection_details,
            ))

    def require_position(constraint_id: str, kind: str, entity_id: str) -> tuple[float, float, float] | None:
        position = resolved.get(entity_id)
        if position is not None:
            return _position_tuple(position)
        rejections.append(_rejection(
            constraint_id, kind, [entity_id],
            'Required entity position is unknown; hard constraint cannot be treated as passed',
        ))
        return None

    for index, constraint in enumerate(spec.constraints):
        if isinstance(constraint, AllowedRegionConstraint):
            region = prepared.region_geometries[index]
            assert region is not None  # prepared for every region constraint
            for entity_id in constraint.entity_ids:
                position = require_position(constraint.constraint_id, constraint.kind, entity_id)
                if position is None:
                    continue
                profile = profiles.get(entity_id)
                envelope = _entity_envelope(Point(position[0], position[1]), profile)
                passed = bool(region.covers(envelope))
                observations.append({
                    'constraint_id': constraint.constraint_id, 'kind': constraint.kind, 'entity_ids': [entity_id],
                    'actual': {'inside_allowed_region': passed, **_profile_observation(profile)},
                    'required': {'inside_allowed_region': True}, 'passed': passed,
                })
                if not passed:
                    rejections.append(_rejection(
                        constraint.constraint_id, constraint.kind, [entity_id],
                        'Entity envelope is not fully covered by the allowed region',
                        **_profile_observation(profile),
                    ))

        elif isinstance(constraint, ExclusionRegionConstraint):
            region = prepared.region_geometries[index]
            assert region is not None  # prepared for every region constraint
            for entity_id in constraint.entity_ids:
                position = require_position(constraint.constraint_id, constraint.kind, entity_id)
                if position is None:
                    continue
                profile = profiles.get(entity_id)
                envelope = _entity_envelope(Point(position[0], position[1]), profile)
                intersects = bool(region.intersects(envelope))
                observations.append({
                    'constraint_id': constraint.constraint_id, 'kind': constraint.kind, 'entity_ids': [entity_id],
                    'actual': {'intersects_exclusion_region': intersects, **_profile_observation(profile)},
                    'required': {'intersects_exclusion_region': False}, 'passed': not intersects,
                })
                if intersects:
                    rejections.append(_rejection(
                        constraint.constraint_id, constraint.kind, [entity_id],
                        'Entity envelope intersects an exclusion region',
                        **_profile_observation(profile),
                    ))

        elif isinstance(constraint, WallClearanceConstraint):
            edge = edges[constraint.edge_id]
            for entity_id in constraint.entity_ids:
                position = require_position(constraint.constraint_id, constraint.kind, entity_id)
                if position is None:
                    continue
                profile = profiles.get(entity_id)
                point = Point(position[0], position[1])
                envelope = _entity_envelope(point, profile)
                center_distance = float(point.distance(edge))
                clearance = float(envelope.distance(edge))
                passed = ((constraint.min_m is None or clearance + _EPS >= constraint.min_m)
                          and (constraint.max_m is None or clearance <= constraint.max_m + _EPS))
                observations.append({
                    'constraint_id': constraint.constraint_id, 'kind': constraint.kind, 'entity_ids': [entity_id],
                    'actual': {'clearance_m': clearance, 'center_distance_m': center_distance, **_profile_observation(profile)},
                    'required': {'edge_id': constraint.edge_id, 'min_m': constraint.min_m, 'max_m': constraint.max_m},
                    'passed': passed,
                })
                if not passed:
                    rejections.append(_rejection(
                        constraint.constraint_id, constraint.kind, [entity_id],
                        'Wall clearance is outside the required range', edge_id=constraint.edge_id,
                        clearance_m=clearance, center_distance_m=center_distance,
                        **_profile_observation(profile),
                        min_m=constraint.min_m, max_m=constraint.max_m,
                    ))

        elif isinstance(constraint, AxisConstraint):
            position = require_position(constraint.constraint_id, constraint.kind, constraint.entity_id)
            if position is None:
                continue
            axis_index = {'x': 0, 'y': 1, 'z': 2}[constraint.axis]
            value = position[axis_index]
            fixed_ok = constraint.fixed_m is None or abs(value - constraint.fixed_m) <= constraint.tolerance_m
            min_ok = constraint.min_m is None or value + _EPS >= constraint.min_m
            max_ok = constraint.max_m is None or value <= constraint.max_m + _EPS
            passed = fixed_ok and min_ok and max_ok
            observations.append({
                'constraint_id': constraint.constraint_id, 'kind': constraint.kind, 'entity_ids': [constraint.entity_id],
                'actual': {'axis': constraint.axis, 'value_m': value},
                'required': {'fixed_m': constraint.fixed_m, 'min_m': constraint.min_m, 'max_m': constraint.max_m,
                             'tolerance_m': constraint.tolerance_m}, 'passed': passed,
            })
            if not passed:
                rejections.append(_rejection(
                    constraint.constraint_id, constraint.kind, [constraint.entity_id],
                    'Axis coordinate is outside the required range', axis=constraint.axis, value_m=value,
                    fixed_m=constraint.fixed_m, min_m=constraint.min_m, max_m=constraint.max_m,
                    tolerance_m=constraint.tolerance_m,
                ))

        elif isinstance(constraint, MovementBudgetConstraint):
            position = require_position(constraint.constraint_id, constraint.kind, constraint.entity_id)
            baseline_raw = baselines[constraint.entity_id]
            if position is None or baseline_raw is None:
                continue
            baseline = _position_tuple(baseline_raw)
            distance = _distance(position, baseline, constraint.distance_mode)
            passed = distance <= constraint.max_distance_m + _EPS
            observations.append({
                'constraint_id': constraint.constraint_id, 'kind': constraint.kind, 'entity_ids': [constraint.entity_id],
                'actual': {'distance_m': distance, 'distance_mode': constraint.distance_mode},
                'required': {'max_distance_m': constraint.max_distance_m}, 'passed': passed,
            })
            if not passed:
                rejections.append(_rejection(
                    constraint.constraint_id, constraint.kind, [constraint.entity_id],
                    'Movement from the Context baseline exceeds the hard budget', distance_m=distance,
                    distance_mode=constraint.distance_mode, max_distance_m=constraint.max_distance_m,
                ))

        elif isinstance(constraint, PairDistanceConstraint):
            a = require_position(constraint.constraint_id, constraint.kind, constraint.entity_a)
            b = require_position(constraint.constraint_id, constraint.kind, constraint.entity_b)
            if a is None or b is None:
                continue
            center_distance = _distance(a, b, constraint.distance_mode)
            profile_a = profiles.get(constraint.entity_a)
            profile_b = profiles.get(constraint.entity_b)
            if constraint.distance_reference == 'envelope_clearance':
                envelope_a = _entity_envelope(Point(a[0], a[1]), profile_a)
                envelope_b = _entity_envelope(Point(b[0], b[1]), profile_b)
                distance = float(envelope_a.distance(envelope_b))
            else:
                distance = center_distance
            passed = ((constraint.min_m is None or distance + _EPS >= constraint.min_m)
                      and (constraint.max_m is None or distance <= constraint.max_m + _EPS))
            observations.append({
                'constraint_id': constraint.constraint_id, 'kind': constraint.kind,
                'entity_ids': [constraint.entity_a, constraint.entity_b],
                'actual': {'distance_m': distance, 'center_distance_m': center_distance, 'distance_mode': constraint.distance_mode,
                           'distance_reference': constraint.distance_reference,
                           'profile_a': _profile_observation(profile_a),
                           'profile_b': _profile_observation(profile_b)},
                'required': {'min_m': constraint.min_m, 'max_m': constraint.max_m}, 'passed': passed,
            })
            if not passed:
                rejections.append(_rejection(
                    constraint.constraint_id, constraint.kind, [constraint.entity_a, constraint.entity_b],
                    'Pair distance is outside the required range', distance_m=distance, center_distance_m=center_distance,
                    distance_mode=constraint.distance_mode, distance_reference=constraint.distance_reference,
                    profile_a=_profile_observation(profile_a),
                    profile_b=_profile_observation(profile_b),
                    min_m=constraint.min_m, max_m=constraint.max_m,
                ))

        elif isinstance(constraint, EntityCollisionConstraint):
            a = require_position(constraint.constraint_id, constraint.kind, constraint.entity_a)
            b = require_position(constraint.constraint_id, constraint.kind, constraint.entity_b)
            if a is None or b is None:
                continue
            profile_a = profiles.get(constraint.entity_a)
            profile_b = profiles.get(constraint.entity_b)
            envelopes_ok = (
                profile_a is not None
                and profile_a.z_extent_m is not None
                and profile_b is not None
                and profile_b.z_extent_m is not None
            )
            if not envelopes_ok:
                observations.append({
                    'constraint_id': constraint.constraint_id, 'kind': constraint.kind,
                    'entity_ids': [constraint.entity_a, constraint.entity_b],
                    'actual': {
                        'envelopes_declared': False,
                        'profile_a': _profile_observation(profile_a),
                        'profile_b': _profile_observation(profile_b),
                    },
                    'required': {'envelopes_declared': True},
                    'passed': False,
                })
                rejections.append(_rejection(
                    constraint.constraint_id, constraint.kind,
                    [constraint.entity_a, constraint.entity_b],
                    'Entity collision cannot be established without declared '
                    '3D envelopes for both entities',
                    profile_a=_profile_observation(profile_a),
                    profile_b=_profile_observation(profile_b),
                ))
                continue
            envelope_a = _entity_envelope(Point(a[0], a[1]), profile_a)
            envelope_b = _entity_envelope(Point(b[0], b[1]), profile_b)
            xy_collide = _envelope_xy_collision(envelope_a, envelope_b)
            a_bottom = a[2] - float(profile_a.z_extent_m)
            a_top = a[2] + float(profile_a.z_extent_m)
            b_bottom = b[2] - float(profile_b.z_extent_m)
            b_top = b[2] + float(profile_b.z_extent_m)
            z_overlap = (min(a_top, b_top) - max(a_bottom, b_bottom)) > _EPS
            collided = xy_collide and z_overlap
            observations.append({
                'constraint_id': constraint.constraint_id, 'kind': constraint.kind,
                'entity_ids': [constraint.entity_a, constraint.entity_b],
                'actual': {
                    'xy_collision': xy_collide,
                    'z_intervals_m': {
                        constraint.entity_a: [a_bottom, a_top],
                        constraint.entity_b: [b_bottom, b_top],
                    },
                    'z_overlap': z_overlap,
                    'profile_a': _profile_observation(profile_a),
                    'profile_b': _profile_observation(profile_b),
                },
                'required': {'xy_collision': False, 'z_overlap': False},
                'passed': not collided,
            })
            if collided:
                rejections.append(_rejection(
                    constraint.constraint_id, constraint.kind,
                    [constraint.entity_a, constraint.entity_b],
                    'Entity envelopes overlap in 3D',
                    xy_collision=xy_collide,
                    z_intervals_m={
                        constraint.entity_a: [a_bottom, a_top],
                        constraint.entity_b: [b_bottom, b_top],
                    },
                ))

        elif isinstance(constraint, LinkedPlacementConstraint):
            a = require_position(constraint.constraint_id, constraint.kind, constraint.entity_a)
            b = require_position(constraint.constraint_id, constraint.kind, constraint.entity_b)
            if a is None or b is None:
                continue
            relation = constraint.relation
            if relation == 'mirror_x':
                actual = (a[0] + b[0]) / 2.0
                target = constraint.mirror_axis_x_m if constraint.mirror_axis_x_m is not None else context.room_width / 2.0
            elif relation.startswith('equal_delta_'):
                axis = {'equal_delta_x': 0, 'equal_delta_y': 1, 'equal_delta_z': 2}[relation]
                baseline_a = _position_tuple(baselines[constraint.entity_a])  # validated at creation
                baseline_b = _position_tuple(baselines[constraint.entity_b])
                actual = (a[axis] - baseline_a[axis]) - (b[axis] - baseline_b[axis])
                target = 0.0
            else:
                axis = {'equal_x': 0, 'equal_y': 1, 'equal_z': 2}[relation]
                actual = a[axis] - b[axis]
                target = 0.0
            error = abs(actual - target)
            passed = error <= constraint.tolerance_m + _EPS
            observations.append({
                'constraint_id': constraint.constraint_id, 'kind': constraint.kind,
                'entity_ids': [constraint.entity_a, constraint.entity_b],
                'actual': {'relation': relation, 'relation_value_m': actual, 'target_m': target, 'error_m': error},
                'required': {'tolerance_m': constraint.tolerance_m}, 'passed': passed,
            })
            if not passed:
                rejections.append(_rejection(
                    constraint.constraint_id, constraint.kind, [constraint.entity_a, constraint.entity_b],
                    'Linked placement relation is not satisfied', relation=relation, relation_value_m=actual,
                    target_m=target, error_m=error, tolerance_m=constraint.tolerance_m,
                ))

    serializable_positions = {entity_id: position for entity_id, position in resolved.items() if position is not None}
    return {
        'classification': 'placement_constraint_evaluation',
        'engine_version': PLACEMENT_CONSTRAINT_ENGINE_VERSION,
        'geometry_version': geometry['geometry_version'],
        'feasible': not rejections,
        'overridden_entity_ids': sorted(overridden),
        'resolved_positions': serializable_positions,
        'checked_constraint_ids': [item.constraint_id for item in spec.constraints],
        'observations': observations,
        'rejections': rejections,
    }


def evaluate_constraint_set(
    context_payload: dict[str, Any],
    raw_spec: dict[str, Any],
    request: PlacementEvaluationRequest,
) -> dict[str, Any]:
    """Single-shot Context+spec evaluation (prepare-once convenience wrapper).

    Batch callers should use ``prepare_constraint_evaluation``/
    ``evaluate_prepared_constraint_set`` so context geometry and spec
    validation are not rebuilt per candidate.
    """
    return evaluate_prepared_constraint_set(
        prepare_constraint_evaluation(context_payload, raw_spec),
        request,
    )
