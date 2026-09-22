from __future__ import annotations

from collections.abc import Iterable
from hashlib import sha256
import json
from math import asin, atan2, cos, degrees, isfinite, radians, sin, sqrt
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_wall_models import WallTopology
from .geometry import polygon_from_vertices
from .semantic_geometry import SemanticAcousticGeometry


class SceneValidationError(ValueError):
    pass


class Position3(BaseModel):
    model_config = ConfigDict(frozen=True)
    x_m: float
    y_m: float
    z_m: float

    @field_validator('x_m', 'y_m', 'z_m')
    @classmethod
    def finite(cls, value: float) -> float:
        value = float(value)
        if not isfinite(value):
            raise ValueError('position values must be finite')
        return value


class Offset3(BaseModel):
    """Entity-local metric offset, kept distinct from a world position."""

    model_config = ConfigDict(frozen=True)
    x_m: float = 0.0
    y_m: float = 0.0
    z_m: float = 0.0

    @field_validator('x_m', 'y_m', 'z_m')
    @classmethod
    def finite(cls, value: float) -> float:
        value = float(value)
        if not isfinite(value):
            raise ValueError('offset values must be finite')
        return value


class Direction3(BaseModel):
    model_config = ConfigDict(frozen=True)
    x: float
    y: float
    z: float

    @model_validator(mode='after')
    def normalized(self) -> 'Direction3':
        values = (float(self.x), float(self.y), float(self.z))
        if any(not isfinite(value) for value in values):
            raise ValueError('direction values must be finite')
        length = sqrt(sum(value * value for value in values))
        if length <= 1e-12:
            raise ValueError('direction must not be zero length')
        if abs(length - 1.0) > 1e-6:
            raise ValueError('direction must be normalized')
        return self


class Quaternion4(BaseModel):
    """Normalized body-pose quaternion (w, x, y, z), independent from speaker aim."""

    model_config = ConfigDict(frozen=True)
    w: float = 1.0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    @model_validator(mode='after')
    def normalized(self) -> 'Quaternion4':
        values = (float(self.w), float(self.x), float(self.y), float(self.z))
        if any(not isfinite(value) for value in values):
            raise ValueError('quaternion values must be finite')
        length = sqrt(sum(value * value for value in values))
        if length <= 1e-12:
            raise ValueError('quaternion must not be zero length')
        if abs(length - 1.0) > 1e-6:
            raise ValueError('quaternion must be normalized')
        return self


IDENTITY_ORIENTATION = Quaternion4()


def normalized_quaternion(w: float, x: float, y: float, z: float) -> Quaternion4:
    values = (float(w), float(x), float(y), float(z))
    if any(not isfinite(value) for value in values):
        raise ValueError('quaternion values must be finite')
    length = sqrt(sum(value * value for value in values))
    if length <= 1e-12:
        raise ValueError('quaternion must not be zero length')
    return Quaternion4(w=values[0] / length, x=values[1] / length, y=values[2] / length, z=values[3] / length)


def quaternion_multiply(left: Quaternion4, right: Quaternion4) -> Quaternion4:
    """Hamilton product. For active rotations, left is applied after right."""

    w1, x1, y1, z1 = left.w, left.x, left.y, left.z
    w2, x2, y2, z2 = right.w, right.x, right.y, right.z
    return normalized_quaternion(
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
    )


def quaternion_from_axis_angle(axis: Literal['x', 'y', 'z'], angle_deg: float) -> Quaternion4:
    half = radians(float(angle_deg)) * 0.5
    c = cos(half)
    s = sin(half)
    if axis == 'x':
        return normalized_quaternion(c, s, 0.0, 0.0)
    if axis == 'y':
        return normalized_quaternion(c, 0.0, s, 0.0)
    if axis == 'z':
        return normalized_quaternion(c, 0.0, 0.0, s)
    raise ValueError(f'unsupported rotation axis: {axis}')


def quaternion_from_euler_deg(*, yaw_deg: float, pitch_deg: float, roll_deg: float) -> Quaternion4:
    """Build intrinsic Z-Y-X yaw/pitch/roll body orientation in HTDT domain axes."""

    yaw = radians(float(yaw_deg)) * 0.5
    pitch = radians(float(pitch_deg)) * 0.5
    roll = radians(float(roll_deg)) * 0.5
    cy, sy = cos(yaw), sin(yaw)
    cp, sp = cos(pitch), sin(pitch)
    cr, sr = cos(roll), sin(roll)
    return normalized_quaternion(
        cr * cp * cy + sr * sp * sy,
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
    )


def quaternion_to_euler_deg(orientation: Quaternion4) -> tuple[float, float, float]:
    """Return intrinsic Z-Y-X yaw, pitch, roll in degrees."""

    w, x, y, z = orientation.w, orientation.x, orientation.y, orientation.z
    sin_yaw = 2.0 * (w * z + x * y)
    cos_yaw = 1.0 - 2.0 * (y * y + z * z)
    yaw = atan2(sin_yaw, cos_yaw)

    sin_pitch = max(-1.0, min(1.0, 2.0 * (w * y - z * x)))
    pitch = asin(sin_pitch)

    sin_roll = 2.0 * (w * x + y * z)
    cos_roll = 1.0 - 2.0 * (x * x + y * y)
    roll = atan2(sin_roll, cos_roll)
    return (degrees(yaw), degrees(pitch), degrees(roll))


def quaternion_to_matrix3(orientation: Quaternion4) -> tuple[tuple[float, float, float], ...]:
    w, x, y, z = orientation.w, orientation.x, orientation.y, orientation.z
    return (
        (1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)),
        (2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)),
        (2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)),
    )


def rotate_orientation_world(
    orientation: Quaternion4,
    axis: Literal['x', 'y', 'z'],
    angle_deg: float,
) -> Quaternion4:
    """Apply a world-axis rotation before the existing body orientation."""

    return quaternion_multiply(quaternion_from_axis_angle(axis, angle_deg), orientation)


def rotate_position_world(
    position: Position3,
    pivot: Position3,
    axis: Literal['x', 'y', 'z'],
    angle_deg: float,
) -> Position3:
    """Rotate a domain position about a world-axis pivot without changing handedness conventions."""

    rotation = quaternion_to_matrix3(quaternion_from_axis_angle(axis, angle_deg))
    delta = (position.x_m - pivot.x_m, position.y_m - pivot.y_m, position.z_m - pivot.z_m)
    rotated = tuple(sum(rotation[row][col] * delta[col] for col in range(3)) for row in range(3))
    return Position3(
        x_m=pivot.x_m + rotated[0],
        y_m=pivot.y_m + rotated[1],
        z_m=pivot.z_m + rotated[2],
    )


class Size3(BaseModel):
    model_config = ConfigDict(frozen=True)
    x_m: float = Field(gt=0)
    y_m: float = Field(gt=0)
    z_m: float = Field(gt=0)


class RoomVertex(BaseModel):
    model_config = ConfigDict(frozen=True)
    vertex_id: str = Field(min_length=1)
    x_m: float
    y_m: float

    @field_validator('x_m', 'y_m')
    @classmethod
    def finite(cls, value: float) -> float:
        value = float(value)
        if not isfinite(value):
            raise ValueError('room vertex values must be finite')
        return value


class RoomPrism(BaseModel):
    """Single-room prism. Legacy rectangles omit footprint_vertices; N30+ rooms store them explicitly."""

    model_config = ConfigDict(frozen=True)
    room_id: str = 'room'
    width_m: float = Field(gt=0)
    depth_m: float = Field(gt=0)
    height_m: float = Field(gt=0)
    footprint_vertices: tuple[RoomVertex, ...] | None = None

    @model_validator(mode='after')
    def valid_footprint(self) -> 'RoomPrism':
        if self.footprint_vertices is None:
            return self
        vertices = self.footprint_vertices
        if len(vertices) < 3:
            raise ValueError('room footprint must have at least three vertices')
        ids = [vertex.vertex_id for vertex in vertices]
        if len(ids) != len(set(ids)):
            raise ValueError('room vertex ids must be unique')
        polygon = polygon_from_vertices([(vertex.x_m, vertex.y_m) for vertex in vertices])
        min_x, min_y, max_x, max_y = (float(value) for value in polygon.bounds)
        expected_width = max_x - min_x
        expected_depth = max_y - min_y
        tolerance = 1e-9
        if abs(float(self.width_m) - expected_width) > tolerance:
            raise ValueError('room width_m must match footprint bounds')
        if abs(float(self.depth_m) - expected_depth) > tolerance:
            raise ValueError('room depth_m must match footprint bounds')
        return self

    @property
    def bounds_m(self) -> tuple[float, float, float, float]:
        if self.footprint_vertices is None:
            return (0.0, 0.0, float(self.width_m), float(self.depth_m))
        xs = [vertex.x_m for vertex in self.footprint_vertices]
        ys = [vertex.y_m for vertex in self.footprint_vertices]
        return (min(xs), min(ys), max(xs), max(ys))


def room_vertices(room: RoomPrism) -> tuple[RoomVertex, ...]:
    if room.footprint_vertices is not None:
        return room.footprint_vertices
    return (
        RoomVertex(vertex_id='front-left', x_m=0.0, y_m=0.0),
        RoomVertex(vertex_id='front-right', x_m=room.width_m, y_m=0.0),
        RoomVertex(vertex_id='rear-right', x_m=room.width_m, y_m=room.depth_m),
        RoomVertex(vertex_id='rear-left', x_m=0.0, y_m=room.depth_m),
    )


def make_polygon_room(
    vertices: Sequence[RoomVertex],
    *,
    height_m: float,
    room_id: str = 'room',
) -> RoomPrism:
    ordered = tuple(vertices)
    if len(ordered) < 3:
        raise ValueError('room footprint must have at least three vertices')
    ids = [vertex.vertex_id for vertex in ordered]
    if len(ids) != len(set(ids)):
        raise ValueError('room vertex ids must be unique')
    polygon = polygon_from_vertices([(vertex.x_m, vertex.y_m) for vertex in ordered])
    min_x, min_y, max_x, max_y = (float(value) for value in polygon.bounds)
    return RoomPrism(
        room_id=room_id,
        width_m=max_x - min_x,
        depth_m=max_y - min_y,
        height_m=float(height_m),
        footprint_vertices=ordered,
    )


PhysicalEntityKind = Literal['speaker', 'seat', 'screen', 'projector', 'riser', 'furniture', 'av_equipment']
EntityKind = Literal[
    'speaker',
    'seat',
    'screen',
    'projector',
    'riser',
    'furniture',
    'av_equipment',
    'measurement_point',
]
PHYSICAL_ENTITY_KINDS = frozenset({
    'speaker',
    'seat',
    'screen',
    'projector',
    'riser',
    'furniture',
    'av_equipment',
})


class SceneEntity(BaseModel):
    model_config = ConfigDict(frozen=True)
    entity_id: str = Field(min_length=1)
    kind: EntityKind
    name: str = Field(min_length=1)
    position: Position3
    orientation: Quaternion4 = Field(default_factory=Quaternion4)
    size_m: Size3 | None = None
    acoustic_reference_offset_m: Offset3 | None = None
    speaker_role: str | None = None
    aim_xyz: Direction3 | None = None

    @model_validator(mode='after')
    def semantic_fields(self) -> 'SceneEntity':
        if self.kind == 'speaker' and not self.speaker_role:
            raise ValueError('speaker_role is required for speakers')
        if self.kind != 'speaker' and (self.speaker_role is not None or self.aim_xyz is not None):
            raise ValueError('speaker fields are only valid for speakers')
        if self.kind in PHYSICAL_ENTITY_KINDS and self.size_m is None:
            raise ValueError(f'size_m is required for physical entity kind {self.kind}')
        if self.kind == 'measurement_point':
            if self.size_m is not None:
                raise ValueError('measurement points do not have physical size_m')
            if self.acoustic_reference_offset_m is not None:
                raise ValueError('measurement points are already acoustic reference positions')
        return self


# --- Speaker channel-role authority -----------------------------------------
#
# SceneEntity keeps its persisted invariant that a speaker carries a non-empty
# speaker_role. The authoring "not assigned yet" state is therefore stored as a
# reserved placeholder token (``UNASSIGNED-<n>``) rather than as None. Every
# token in that family — plus the legacy ``SPK`` default written by older
# builds — denotes "no channel role chosen yet" and must never be presented or
# consumed as a real channel identity. Uniqueness of the suffix keeps each
# unassigned speaker distinguishable and prevents downstream authorities that
# require unique role ids (SystemVariant role bindings, topology final roles)
# from collapsing several placeholders into one fake channel.
UNASSIGNED_SPEAKER_ROLE_PREFIX = 'UNASSIGNED'
LEGACY_UNASSIGNED_SPEAKER_ROLES = frozenset({'SPK'})


def is_unassigned_speaker_role(role: str | None) -> bool:
    """True when ``role`` is missing or is a reserved authoring placeholder."""

    if role is None:
        return True
    normalized = role.strip()
    return (
        not normalized
        or normalized in LEGACY_UNASSIGNED_SPEAKER_ROLES
        or normalized == UNASSIGNED_SPEAKER_ROLE_PREFIX
        or normalized.startswith(f'{UNASSIGNED_SPEAKER_ROLE_PREFIX}-')
    )


def next_unassigned_speaker_role(existing_roles: Iterable[str | None]) -> str:
    """Smallest free reserved placeholder token across the given roles."""

    taken = {
        role.strip()
        for role in existing_roles
        if role is not None and role.strip()
    }
    index = 1
    while f'{UNASSIGNED_SPEAKER_ROLE_PREFIX}-{index}' in taken:
        index += 1
    return f'{UNASSIGNED_SPEAKER_ROLE_PREFIX}-{index}'


def duplicated_speaker_roles(entities: Iterable[SceneEntity]) -> tuple[str, ...]:
    """Sorted exclusive channel roles claimed by more than one speaker.

    Unassigned placeholders are ignored: they are reported through the
    missing-role path instead of being counted as duplicate channel claims.
    """

    counts: dict[str, int] = {}
    for entity in entities:
        role = getattr(entity, 'speaker_role', None)
        if getattr(entity, 'kind', None) != 'speaker' or is_unassigned_speaker_role(role):
            continue
        normalized = (role or '').strip()
        if not normalized:
            continue
        counts[normalized] = counts.get(normalized, 0) + 1
    return tuple(sorted(role for role, count in counts.items() if count > 1))


def acoustic_reference_position(entity: SceneEntity) -> Position3 | None:
    """Resolve an entity-local acoustic reference into world HTDT coordinates.

    Standalone measurement points are already world reference positions. Physical
    objects only expose a reference when an explicit local offset is present.
    """

    if entity.kind == 'measurement_point':
        return entity.position
    offset = entity.acoustic_reference_offset_m
    if offset is None:
        return None
    matrix = quaternion_to_matrix3(entity.orientation)
    local = (offset.x_m, offset.y_m, offset.z_m)
    rotated = tuple(sum(matrix[row][column] * local[column] for column in range(3)) for row in range(3))
    return Position3(
        x_m=entity.position.x_m + rotated[0],
        y_m=entity.position.y_m + rotated[1],
        z_m=entity.position.z_m + rotated[2],
    )


class SceneDocument(BaseModel):
    model_config = ConfigDict(frozen=True)
    document_id: str = Field(min_length=1)
    schema_version: int = 1
    coordinate_system: Literal['htdt-x-right-y-rear-z-up-m'] = 'htdt-x-right-y-rear-z-up-m'
    room: RoomPrism | None
    wall_topology: WallTopology | None = None
    r120_semantic_geometry: SemanticAcousticGeometry | None = None
    entities: tuple[SceneEntity, ...]

    @model_validator(mode='after')
    def valid_document(self) -> 'SceneDocument':
        ids = [entity.entity_id for entity in self.entities]
        if len(ids) != len(set(ids)):
            raise ValueError('entity_id values must be unique')
        if self.wall_topology is not None:
            if self.room is None:
                raise ValueError('wall topology requires a room')
            if self.schema_version < 3:
                raise ValueError('wall topology requires scene schema_version >= 3')
            from .cad_walls import validate_wall_topology

            validate_wall_topology(self.room, self.wall_topology)
        if self.r120_semantic_geometry is not None and self.schema_version < 4:
            raise ValueError('R120 semantic geometry requires scene schema_version >= 4')
        return self

    def entity(self, entity_id: str) -> SceneEntity:
        for entity in self.entities:
            if entity.entity_id == entity_id:
                return entity
        raise KeyError(entity_id)


def canonical_scene_json(document: SceneDocument) -> str:
    payload = document.model_dump(mode='json')
    # Optional raw-mesh repair lineage is omitted when absent so pre-Issue-167
    # semantic geometry and SceneRevision hashes remain byte-for-byte canonical.
    semantic_geometry = payload.get('r120_semantic_geometry')
    if isinstance(semantic_geometry, dict):
        request = semantic_geometry.get('conversion_request')
        if isinstance(request, dict) and request.get('raw_mesh_repair_lineage') is None:
            request.pop('raw_mesh_repair_lineage', None)
    # Preserve hashes of N05/N10 identity-pose revisions: identity orientation is canonical omission.
    for entity in payload['entities']:
        orientation = entity.get('orientation')
        if orientation == IDENTITY_ORIENTATION.model_dump(mode='json'):
            entity.pop('orientation', None)
        # N40 adds an optional body-local reference; omission preserves older scene hashes.
        if entity.get('acoustic_reference_offset_m') is None:
            entity.pop('acoustic_reference_offset_m', None)
    # Preserve N05/N10/N20 rectangular-room hashes by omitting the new optional field.
    if isinstance(payload.get('room'), dict) and payload['room'].get('footprint_vertices') is None:
        payload['room'].pop('footprint_vertices', None)
    # Preserve N05-N30a hashes until a wall topology is explicitly created.
    if payload.get('wall_topology') is None:
        payload.pop('wall_topology', None)
    # Preserve all pre-R120B hashes until semantic acoustic geometry is explicitly bound.
    if payload.get('r120_semantic_geometry') is None:
        payload.pop('r120_semantic_geometry', None)
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def scene_content_hash(document: SceneDocument) -> str:
    return sha256(canonical_scene_json(document).encode('utf-8')).hexdigest()


def domain_to_render(position: Position3) -> tuple[float, float, float]:
    """Map HTDT +X right/+Y rear/+Z up into VTK's right-handed world."""

    return (position.x_m, -position.y_m, position.z_m)


def domain_pose_to_render_matrix(
    position: Position3,
    orientation: Quaternion4,
) -> tuple[tuple[float, float, float, float], ...]:
    """Convert an active HTDT pose with Tv=C4*Td*C4, C=diag(1,-1,1)."""

    domain = quaternion_to_matrix3(orientation)
    signs = (1.0, -1.0, 1.0)
    render_rotation = tuple(
        tuple(signs[row] * domain[row][column] * signs[column] for column in range(3))
        for row in range(3)
    )
    tx, ty, tz = domain_to_render(position)
    return (
        (render_rotation[0][0], render_rotation[0][1], render_rotation[0][2], tx),
        (render_rotation[1][0], render_rotation[1][1], render_rotation[1][2], ty),
        (render_rotation[2][0], render_rotation[2][1], render_rotation[2][2], tz),
        (0.0, 0.0, 0.0, 1.0),
    )


def render_delta_to_domain(delta_xyz: tuple[float, float, float], base: Position3) -> Position3:
    dx, dy, dz = (float(value) for value in delta_xyz)
    return Position3(x_m=base.x_m + dx, y_m=base.y_m - dy, z_m=base.z_m + dz)


F1_DOCUMENT_ID = 'fixture-f1'


def make_empty_scene(document_id: str) -> SceneDocument:
    return SceneDocument(document_id=document_id, schema_version=2, room=None, entities=())


def make_f1_scene() -> SceneDocument:
    return SceneDocument(
        document_id=F1_DOCUMENT_ID,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='Front Left',
                speaker_role='FL',
                position=Position3(x_m=1.35, y_m=0.75, z_m=1.05),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
                aim_xyz=None,
            ),
            SceneEntity(
                entity_id='speaker-c',
                kind='speaker',
                name='Center',
                speaker_role='C',
                position=Position3(x_m=3.0, y_m=0.55, z_m=0.85),
                size_m=Size3(x_m=0.50, y_m=0.28, z_m=0.20),
                aim_xyz=None,
            ),
            SceneEntity(
                entity_id='speaker-fr',
                kind='speaker',
                name='Front Right',
                speaker_role='FR',
                position=Position3(x_m=4.65, y_m=0.75, z_m=1.05),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
                aim_xyz=Direction3(x=-0.514496, y=0.857493, z=0.0),
            ),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
            SceneEntity(
                entity_id='furniture-left',
                kind='furniture',
                name='Left cabinet',
                position=Position3(x_m=0.55, y_m=2.2, z_m=0.45),
                size_m=Size3(x_m=0.8, y_m=0.45, z_m=0.9),
            ),
        ),
    )