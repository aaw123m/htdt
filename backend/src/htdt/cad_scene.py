from __future__ import annotations

from collections.abc import Iterable
from hashlib import sha256
import json
from math import asin, atan2, cos, degrees, isfinite, radians, sin, sqrt
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shapely.geometry import Polygon
from shapely.validation import explain_validity

from .cad_attachment_models import ConstructionAssembly, EntityAttachment
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


def quaternion_from_axis_angle_vector(
    axis_xyz: tuple[float, float, float],
    angle_deg: float,
) -> Quaternion4:
    """Rotation about an arbitrary world-frame axis (right-hand rule)."""

    ax, ay, az = (float(value) for value in axis_xyz)
    length = sqrt(ax * ax + ay * ay + az * az)
    if length <= 1e-12:
        raise ValueError('rotation axis must not be zero length')
    ax, ay, az = ax / length, ay / length, az / length
    half = radians(float(angle_deg)) * 0.5
    c = cos(half)
    s = sin(half)
    return normalized_quaternion(c, s * ax, s * ay, s * az)


def quaternion_from_matrix3(matrix: tuple[tuple[float, float, float], ...]) -> Quaternion4:
    """Recover a quaternion from a proper rotation matrix (numerically stable).

    Columns are world-basis images of the local axes, matching the layout
    produced by :func:`quaternion_to_matrix3` (world = R · local). The caller
    must pass an orthonormal rotation (determinant +1); reflected or
    degenerate input fails closed.
    """

    m = tuple(
        tuple(float(matrix[row][column]) for column in range(3))
        for row in range(3)
    )
    for row in m:
        if any(not isfinite(value) for value in row):
            raise ValueError('rotation matrix values must be finite')
    determinant = (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )
    if abs(determinant - 1.0) > 1e-3:
        raise ValueError('matrix is not a proper rotation (det != +1)')
    trace = m[0][0] + m[1][1] + m[2][2]
    if trace > 0.0:
        scale = sqrt(trace + 1.0) * 2.0
        return normalized_quaternion(
            0.25 * scale,
            (m[2][1] - m[1][2]) / scale,
            (m[0][2] - m[2][0]) / scale,
            (m[1][0] - m[0][1]) / scale,
        )
    if m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        scale = sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2.0
        return normalized_quaternion(
            (m[2][1] - m[1][2]) / scale,
            0.25 * scale,
            (m[0][1] + m[1][0]) / scale,
            (m[0][2] + m[2][0]) / scale,
        )
    if m[1][1] > m[2][2]:
        scale = sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2.0
        return normalized_quaternion(
            (m[0][2] - m[2][0]) / scale,
            (m[0][1] + m[1][0]) / scale,
            0.25 * scale,
            (m[1][2] + m[2][1]) / scale,
        )
    scale = sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2.0
    return normalized_quaternion(
        (m[1][0] - m[0][1]) / scale,
        (m[0][2] + m[2][0]) / scale,
        (m[1][2] + m[2][1]) / scale,
        0.25 * scale,
    )


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


# --- Physical body geometry (Issue #464) -------------------------------------
#
# ``size_m`` remains the persisted bounding envelope used for broad-phase and
# legacy consumers. ``EntityBodyGeometry`` optionally refines the authored body
# shape for display and collision. Body geometry is never acoustic solver
# authority: acoustic geometry enters a scene only through
# ``SceneDocument.r120_semantic_geometry``.

BodyGeometryKind = Literal['box', 'cylinder', 'extruded_polygon', 'mesh_asset']
MeshAssetFormat = Literal['obj', 'glb', 'ply', 'stl', 'htdt_meshbin_v1']

_ENTITY_FOOTPRINT_MIN_AREA_M2 = 1e-8
_ENVELOPE_FIT_EPS = 1e-9


class FootprintVertex(BaseModel):
    """Entity-local XY footprint vertex in meters (origin = entity anchor)."""

    model_config = ConfigDict(frozen=True)
    x_m: float
    y_m: float

    @field_validator('x_m', 'y_m')
    @classmethod
    def finite(cls, value: float) -> float:
        value = float(value)
        if not isfinite(value):
            raise ValueError('footprint vertex values must be finite')
        return value


class BodyMeshVertex(BaseModel):
    """Entity-local mesh vertex in meters."""

    model_config = ConfigDict(frozen=True)
    x_m: float
    y_m: float
    z_m: float

    @field_validator('x_m', 'y_m', 'z_m')
    @classmethod
    def finite(cls, value: float) -> float:
        value = float(value)
        if not isfinite(value):
            raise ValueError('body mesh vertex values must be finite')
        return value


class BodyMeshTriangle(BaseModel):
    model_config = ConfigDict(frozen=True)
    a: int = Field(ge=0)
    b: int = Field(ge=0)
    c: int = Field(ge=0)

    @model_validator(mode='after')
    def distinct_indices(self) -> 'BodyMeshTriangle':
        if len({self.a, self.b, self.c}) != 3:
            raise ValueError('body mesh triangle indices must be distinct')
        return self


# --- Mesh import authority (Issue #669) --------------------------------------
#
# Imported vertex coordinates are source units, never implicitly meters. The
# persisted ``MeshImportAuthority`` records exactly which unit/axis/anchor
# interpretation made the entity-local meter geometry authoritative. A missing
# authority means the mesh predates explicit import authority
# (``legacy_assumed_meter``) and must never be silently reinterpreted.

MeshImportSourceUnit = Literal[
    'meters', 'millimeters', 'centimeters', 'inches', 'feet', 'custom', 'unknown'
]
MeshImportUnitDeclaredBy = Literal[
    'format_specification',
    'operator_confirmed',
    'format_contract',
    'legacy_assumed_meter',
    'undeclared',
]
MeshImportAxisLabel = Literal['x+', 'x-', 'y+', 'y-', 'z+', 'z-', 'unknown']
MeshImportAnchor = Literal[
    'source_origin', 'bounds_center', 'bottom_center', 'equipment_reference', 'custom_offset'
]


class MeshImportAuthority(BaseModel):
    """Exact source-unit/axis/anchor provenance for imported mesh geometry.

    Persisted with the mesh body so the source-coordinates → HTDT
    entity-local-meters transform is inspectable authority rather than a
    silent decode-time assumption.
    """

    model_config = ConfigDict(frozen=True)
    source_unit: MeshImportSourceUnit
    unit_declared_by: MeshImportUnitDeclaredBy
    custom_scale_to_meters: float | None = Field(default=None, gt=0)
    source_up_axis: MeshImportAxisLabel = 'unknown'
    source_forward_axis: MeshImportAxisLabel = 'unknown'
    handedness: Literal['right', 'left', 'unknown'] = 'unknown'
    convention_declared_by: Literal[
        'format_specification', 'operator_confirmed', 'legacy_assumed', 'undeclared'
    ] = 'format_specification'
    local_anchor: MeshImportAnchor = 'source_origin'
    anchor_offset_m: Offset3 | None = None
    importer_id: str = Field(min_length=1)
    importer_version: str = Field(min_length=1)

    @model_validator(mode='after')
    def consistent(self) -> 'MeshImportAuthority':
        if self.source_unit == 'custom' and self.custom_scale_to_meters is None:
            raise ValueError('custom source unit requires custom_scale_to_meters')
        if self.source_unit != 'custom' and self.custom_scale_to_meters is not None:
            raise ValueError('custom_scale_to_meters is only valid for custom source units')
        if self.unit_declared_by == 'operator_confirmed' and self.source_unit == 'unknown':
            raise ValueError('operator-confirmed import authority must resolve a concrete unit')
        if self.unit_declared_by == 'undeclared' and self.source_unit != 'unknown':
            raise ValueError('an undeclared import authority cannot claim a concrete unit')
        if self.local_anchor != 'custom_offset' and self.anchor_offset_m is not None:
            raise ValueError('anchor_offset_m is only valid for a custom_offset local anchor')
        if self.local_anchor == 'custom_offset' and self.anchor_offset_m is None:
            raise ValueError('custom_offset local anchor requires anchor_offset_m')
        return self


class BodyMeshAsset(BaseModel):
    """Imported mesh body bound to the entity-local frame.

    The mesh is explicit local-coordinate geometry with immutable asset
    provenance (``asset_sha256`` addresses the original bytes in the project
    content blob store). It is display and collision-envelope authority only —
    never acoustic solver geometry.
    """

    model_config = ConfigDict(frozen=True)
    asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_name: str = Field(min_length=1)
    asset_format: MeshAssetFormat
    original_size_bytes: int = Field(ge=0)
    # Local transform: vertices are scaled by ``uniform_scale`` then translated
    # by ``local_offset_m`` inside the entity-local frame.
    local_offset_m: Offset3 = Field(default_factory=Offset3)
    uniform_scale: float = Field(default=1.0, gt=0)
    vertices: tuple[BodyMeshVertex, ...] = Field(min_length=1)
    triangles: tuple[BodyMeshTriangle, ...] = Field(min_length=1)
    # Issue #669: absent authority = pre-authority-v2 legacy import; never
    # silently reinterpreted as a freshly declared unit.
    import_authority: MeshImportAuthority | None = None

    @model_validator(mode='after')
    def valid_mesh(self) -> 'BodyMeshAsset':
        vertex_count = len(self.vertices)
        for triangle in self.triangles:
            if max(triangle.a, triangle.b, triangle.c) >= vertex_count:
                raise ValueError('body mesh triangle references an unknown vertex')
        return self

    def transformed_bounds(self) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        """Entity-local min/max bounds after ``uniform_scale`` + ``local_offset_m``."""

        scale = float(self.uniform_scale)
        offset = self.local_offset_m
        xs, ys, zs = [], [], []
        for vertex in self.vertices:
            xs.append(float(vertex.x_m) * scale + offset.x_m)
            ys.append(float(vertex.y_m) * scale + offset.y_m)
            zs.append(float(vertex.z_m) * scale + offset.z_m)
        return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


class BodyMeshReference(BaseModel):
    """Compact Scene reference to a content-addressed normalized mesh body (Issue #653).

    The immutable normalized geometry lives once in the project content blob
    store under ``geometry_asset_sha256`` (``htdt_meshbin_v1`` bytes). A
    SceneRevision binds exact mesh content by digest instead of embedding the
    vertex arrays inline; ``bounds_min_m``/``bounds_max_m`` are the transformed
    entity-local bounds so envelope validation and broad-phase checks never
    require decoding the blob.
    """

    model_config = ConfigDict(frozen=True)
    geometry_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    geometry_format: Literal['htdt_meshbin_v1'] = 'htdt_meshbin_v1'
    source_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_name: str = Field(min_length=1)
    source_format: MeshAssetFormat
    original_size_bytes: int = Field(ge=0)
    # Same local transform semantics as BodyMeshAsset: stored vertices are
    # scaled by ``uniform_scale`` then translated by ``local_offset_m``.
    local_offset_m: Offset3 = Field(default_factory=Offset3)
    uniform_scale: float = Field(default=1.0, gt=0)
    bounds_min_m: Position3
    bounds_max_m: Position3
    vertex_count: int = Field(ge=1)
    triangle_count: int = Field(ge=1)
    import_authority: MeshImportAuthority | None = None

    def transformed_bounds(self) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        minimum = self.bounds_min_m
        maximum = self.bounds_max_m
        return (minimum.x_m, minimum.y_m, minimum.z_m), (maximum.x_m, maximum.y_m, maximum.z_m)


class EntityBodyGeometry(BaseModel):
    """Optional refined body shape for a physical SceneEntity.

    Kinds:
    - ``box``: explicit opt-in to the legacy rectangular envelope.
    - ``cylinder``: vertical circular prism of ``radius_m`` spanning the full
      ``size_m.z_m`` extent (round tables, cylindrical cabinets).
    - ``extruded_polygon``: arbitrary entity-local XY ``footprint_vertices``
      extruded over the full ``size_m.z_m`` extent (L-shaped sofas, risers,
      irregular cabinets).
    - ``mesh_asset``: imported entity-local triangle mesh. The geometry is
      carried either inline (``mesh``, legacy form) or as a compact
      ``BodyMeshReference`` into the project content blob store (``mesh`` is
      then an in-memory resolution cache and is not serialized).
    """

    model_config = ConfigDict(frozen=True)
    kind: BodyGeometryKind
    radius_m: float | None = Field(default=None, gt=0)
    footprint_vertices: tuple[FootprintVertex, ...] | None = None
    mesh: BodyMeshAsset | None = None
    mesh_reference: BodyMeshReference | None = None

    @model_validator(mode='after')
    def valid_shape(self) -> 'EntityBodyGeometry':
        if self.kind == 'box':
            if (
                self.radius_m is not None
                or self.footprint_vertices is not None
                or self.mesh is not None
                or self.mesh_reference is not None
            ):
                raise ValueError('box body geometry carries no additional parameters')
        elif self.kind == 'cylinder':
            if self.radius_m is None:
                raise ValueError('cylinder body geometry requires radius_m')
            if self.footprint_vertices is not None or self.mesh is not None or self.mesh_reference is not None:
                raise ValueError('cylinder body geometry only accepts radius_m')
        elif self.kind == 'extruded_polygon':
            if self.footprint_vertices is None:
                raise ValueError('extruded_polygon body geometry requires footprint_vertices')
            if self.radius_m is not None or self.mesh is not None or self.mesh_reference is not None:
                raise ValueError('extruded_polygon body geometry only accepts footprint_vertices')
            self.footprint_polygon()
        elif self.kind == 'mesh_asset':
            if self.mesh is None and self.mesh_reference is None:
                raise ValueError('mesh_asset body geometry requires mesh or mesh_reference')
            if self.radius_m is not None or self.footprint_vertices is not None:
                raise ValueError('mesh_asset body geometry only accepts mesh geometry')
            if self.mesh is not None and self.mesh_reference is not None:
                if self.mesh.asset_sha256 != self.mesh_reference.source_asset_sha256:
                    raise ValueError('resolved mesh does not match its mesh_reference provenance')
                if (
                    float(self.mesh.uniform_scale) != float(self.mesh_reference.uniform_scale)
                    or self.mesh.local_offset_m != self.mesh_reference.local_offset_m
                ):
                    raise ValueError('resolved mesh transform does not match mesh_reference')
        return self

    def footprint_polygon(self) -> Polygon:
        """Validate and return the authored entity-local footprint polygon."""

        if self.footprint_vertices is None:
            raise ValueError('body geometry has no footprint polygon')
        coords = tuple(
            (float(vertex.x_m), float(vertex.y_m))
            for vertex in self.footprint_vertices
        )
        if len(coords) < 3:
            raise ValueError('entity footprint polygon must have at least three vertices')
        if coords[0] == coords[-1]:
            raise ValueError(
                'entity footprint polygon must not repeat the first vertex as a closing vertex'
            )
        if len(set(coords)) != len(coords):
            raise ValueError('entity footprint polygon contains duplicate vertices')
        polygon = Polygon(coords)
        if not polygon.is_valid:
            raise ValueError(f'Invalid entity footprint polygon: {explain_validity(polygon)}')
        if polygon.is_empty or polygon.area <= _ENTITY_FOOTPRINT_MIN_AREA_M2:
            raise ValueError('entity footprint polygon area is too small')
        return polygon

    def mesh_bounds_m(self) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
        """Transformed entity-local bounds of the mesh body, when carried."""

        if self.mesh is not None:
            return self.mesh.transformed_bounds()
        if self.mesh_reference is not None:
            return self.mesh_reference.transformed_bounds()
        return None

    def validate_envelope_fit(self, size_m: Size3) -> None:
        """Ensure the authored body stays inside the ``size_m`` bounding envelope.

        ``mesh_reference`` bodies are validated hard against their recorded
        transformed bounds (they only exist in the post-#653 authority
        regime). Legacy inline ``mesh`` bodies are intentionally NOT rejected
        here: pre-authority revisions must remain readable; their containment
        status is surfaced separately via ``mesh_body_envelope_state``.
        """

        if self.kind == 'cylinder':
            limit = min(float(size_m.x_m), float(size_m.y_m)) * 0.5 + _ENVELOPE_FIT_EPS
            if float(self.radius_m) > limit:  # type: ignore[arg-type]
                raise ValueError('cylinder radius exceeds the size_m bounding envelope')
        elif self.kind == 'extruded_polygon':
            half_x = float(size_m.x_m) * 0.5 + _ENVELOPE_FIT_EPS
            half_y = float(size_m.y_m) * 0.5 + _ENVELOPE_FIT_EPS
            for vertex in self.footprint_vertices or ():
                if abs(float(vertex.x_m)) > half_x or abs(float(vertex.y_m)) > half_y:
                    raise ValueError(
                        'entity footprint polygon extends outside the size_m bounding envelope'
                    )
        elif self.kind == 'mesh_asset' and self.mesh_reference is not None:
            minimum, maximum = self.mesh_reference.transformed_bounds()
            half = (
                float(size_m.x_m) * 0.5 + _ENVELOPE_FIT_EPS,
                float(size_m.y_m) * 0.5 + _ENVELOPE_FIT_EPS,
                float(size_m.z_m) * 0.5 + _ENVELOPE_FIT_EPS,
            )
            for axis, (low, high, limit) in enumerate(zip(minimum, maximum, half)):
                if abs(low) > limit or abs(high) > limit:
                    raise ValueError(
                        'mesh body extends outside the size_m bounding envelope '
                        f'(axis {axis}: [{low}, {high}] vs +-{limit - _ENVELOPE_FIT_EPS})'
                    )


def mesh_body_envelope_state(
    body_geometry: 'EntityBodyGeometry | None',
    size_m: Size3,
) -> Literal['contained', 'envelope_unverified', 'not_applicable']:
    """Containment status of a mesh body inside the ``size_m`` envelope (Issue #656).

    ``contained`` — transformed mesh bounds provably fit the envelope.
    ``envelope_unverified`` — a mesh body exists but is not provably inside
    ``size_m`` (legacy inline meshes that exceed the envelope, or any state
    where bounds cannot be established): the ``size_m`` envelope must not be
    treated as a safe broad-phase proxy and the entity is visibly non-safe
    for collision claims until reconciled.
    ``not_applicable`` — no mesh body present.
    """

    if body_geometry is None or body_geometry.kind != 'mesh_asset':
        return 'not_applicable'
    bounds = body_geometry.mesh_bounds_m()
    if bounds is None:
        return 'envelope_unverified'
    minimum, maximum = bounds
    half = (
        float(size_m.x_m) * 0.5 + _ENVELOPE_FIT_EPS,
        float(size_m.y_m) * 0.5 + _ENVELOPE_FIT_EPS,
        float(size_m.z_m) * 0.5 + _ENVELOPE_FIT_EPS,
    )
    for low, high, limit in zip(minimum, maximum, half):
        if abs(low) > limit or abs(high) > limit:
            return 'envelope_unverified'
    return 'contained'


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


PhysicalEntityKind = Literal[
    'speaker',
    'seat',
    'screen',
    'projector',
    'display',
    'riser',
    'furniture',
    'av_equipment',
]
EntityKind = Literal[
    'speaker',
    'seat',
    'screen',
    'projector',
    'display',
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
    'display',
    'riser',
    'furniture',
    'av_equipment',
})


OperationalZoneKind = Literal[
    'door_swing', 'recline', 'slide_out', 'rotate', 'service_access'
]


class OperationalZone(BaseModel):
    """A swept-clearance zone an entity needs in normal operation (Issue #651).

    Persisted per-entity; interpreted in the entity-local frame. Parameters
    are kind-scoped, mirroring ``EntityBodyGeometry``'s per-kind fields.
    """

    model_config = ConfigDict(frozen=True)
    zone_id: str = Field(min_length=1)
    kind: OperationalZoneKind
    # door_swing: hinge offset in entity-local XY + sweep angle.
    hinge_offset_m: tuple[float, float] | None = None
    angle_deg: float | None = Field(default=None, gt=0)
    # recline/slide_out/rotate/service_access: direction + travel extent.
    direction: tuple[float, float] | None = None  # unit-length XY direction
    distance_m: float | None = Field(default=None, gt=0)
    radius_m: float | None = Field(default=None, gt=0)
    height_min_m: float = 0.0
    height_max_m: float | None = None
    label: str | None = None

    @model_validator(mode='after')
    def valid_zone(self) -> 'OperationalZone':
        if self.kind == 'door_swing':
            if self.hinge_offset_m is None or self.angle_deg is None or self.radius_m is None:
                raise ValueError('door_swing requires hinge_offset_m, angle_deg, radius_m')
        elif self.kind == 'rotate':
            if self.radius_m is None or self.angle_deg is None:
                raise ValueError('rotate requires radius_m and angle_deg')
        elif self.kind == 'recline':
            if self.direction is None or self.distance_m is None:
                raise ValueError('recline requires direction and distance_m')
        elif self.kind in ('slide_out', 'service_access'):
            if self.direction is None or self.distance_m is None:
                raise ValueError(f'{self.kind} requires direction and distance_m')
        if self.direction is not None:
            length = (self.direction[0] ** 2 + self.direction[1] ** 2) ** 0.5
            if abs(length - 1.0) > 1e-6:
                raise ValueError('operational zone direction must be unit length')
        if self.height_min_m < 0:
            raise ValueError('zone height_min_m must be non-negative')
        if self.height_max_m is not None and self.height_max_m <= self.height_min_m:
            raise ValueError('zone height_max_m must exceed height_min_m')
        return self


class SemanticCapabilityBinding(BaseModel):
    """Typed semantic capability bound to an entity (Issue #641).

    This is the scene-entity extensibility boundary: an entity gains typed
    capability bindings (``acoustic_source``, ``mountable``, ...) with
    per-capability parameters instead of forcing new shapes of object into a
    closed ``kind`` enum and an ever-growing base record. Bindings are
    validated against the capability registry in
    ``cad_semantic_bindings`` at write time.
    """

    model_config = ConfigDict(frozen=True)
    capability: str = Field(min_length=1)
    parameters: dict[str, object] = Field(default_factory=dict)


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
    body_geometry: EntityBodyGeometry | None = None
    semantic_bindings: tuple[SemanticCapabilityBinding, ...] | None = None
    operational_zones: tuple[OperationalZone, ...] | None = None

    @model_validator(mode='after')
    def semantic_fields(self) -> 'SceneEntity':
        if not self.name.strip():
            raise ValueError('name must not be blank')
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
        if self.body_geometry is not None:
            if self.kind not in PHYSICAL_ENTITY_KINDS:
                raise ValueError('body_geometry is only valid for physical entity kinds')
            if self.size_m is not None:
                self.body_geometry.validate_envelope_fit(self.size_m)
        if self.semantic_bindings is not None:
            if not self.semantic_bindings:
                raise ValueError('semantic_bindings must be omitted when empty')
            capabilities = [binding.capability for binding in self.semantic_bindings]
            if len(capabilities) != len(set(capabilities)):
                raise ValueError('duplicate semantic capability bindings are not allowed')
        if self.operational_zones is not None:
            if not self.operational_zones:
                raise ValueError('operational_zones must be omitted when empty')
            if self.kind not in PHYSICAL_ENTITY_KINDS:
                raise ValueError('operational zones are only valid for physical entity kinds')
            zone_ids = [zone.zone_id for zone in self.operational_zones]
            if len(zone_ids) != len(set(zone_ids)):
                raise ValueError('duplicate operational zone ids are not allowed')
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


ReceiverReferenceKind = Literal[
    'measurement_point',
    'seat_listening_reference',
    'entity_acoustic_reference',
]

_RECEIVER_REFERENCE_LABELS: dict[str, str] = {
    'measurement_point': '測定点',
    'seat_listening_reference': '座席耳基準',
    'entity_acoustic_reference': '音響基準点',
}


def is_listener_receiver_eligible(entity: SceneEntity) -> bool:
    """Listener/receiver eligibility: a speaker is a source, never a receiver.

    Source/receiver/listener semantics are separate (#475): a speaker's
    acoustic reference point exists for directivity and wave excitation — it
    is not a listening position. Source-as-receiver remains possible only
    through an explicit diagnostic/authoring path, never a default picker.
    """

    return entity.kind != 'speaker' and acoustic_reference_position(entity) is not None


def is_measurement_target_eligible(entity: SceneEntity) -> bool:
    """Measurement targets are listener positions; speakers are excluded (#475)."""

    return is_listener_receiver_eligible(entity)


def receiver_reference_kind(entity: SceneEntity) -> ReceiverReferenceKind | None:
    """Semantic kind of an entity's listener acoustic reference, if eligible."""

    if not is_listener_receiver_eligible(entity):
        return None
    if entity.kind == 'measurement_point':
        return 'measurement_point'
    if entity.kind == 'seat':
        return 'seat_listening_reference'
    return 'entity_acoustic_reference'


def receiver_option_label(entity: SceneEntity) -> str | None:
    """Picker label carrying reference provenance (``name · kind · semantics``)."""

    kind = receiver_reference_kind(entity)
    if kind is None:
        return None
    return f"{entity.name} · {entity.kind} · {_RECEIVER_REFERENCE_LABELS[kind]}"


class SceneDocument(BaseModel):
    model_config = ConfigDict(frozen=True)
    document_id: str = Field(min_length=1)
    schema_version: int = 1
    coordinate_system: Literal['htdt-x-right-y-rear-z-up-m'] = 'htdt-x-right-y-rear-z-up-m'
    room: RoomPrism | None
    wall_topology: WallTopology | None = None
    r120_semantic_geometry: SemanticAcousticGeometry | None = None
    entities: tuple[SceneEntity, ...]
    # Issue #661: physical attachment graph (stands, mounts, racks).
    attachments: tuple[EntityAttachment, ...] | None = None
    # Issue #657: unified wall/floor/ceiling physical-layer assemblies.
    construction_assemblies: tuple[ConstructionAssembly, ...] | None = None

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
        if self.attachments is not None:
            if not self.attachments:
                raise ValueError('attachments must be omitted when empty')
            if self.schema_version < 5:
                raise ValueError('entity attachments require scene schema_version >= 5')
            # Issue #661 contract: malformed graphs fail closed at document
            # validation, never at render time.
            from .physical_attachment import attachment_graph

            attachment_graph(self)
        if self.construction_assemblies is not None:
            if not self.construction_assemblies:
                raise ValueError('construction_assemblies must be omitted when empty')
            if self.schema_version < 5:
                raise ValueError('construction assemblies require scene schema_version >= 5')
            from .cad_construction_assembly import validate_construction_assemblies

            validate_construction_assemblies(self)
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
        # Issue #641: optional typed capability bindings; omission preserves
        # pre-#641 scene hashes.
        if entity.get('semantic_bindings') is None:
            entity.pop('semantic_bindings', None)
        if entity.get('operational_zones') is None:
            entity.pop('operational_zones', None)
        # Issue-464 body geometry is optional; omission preserves pre-464 hashes.
        body_geometry = entity.get('body_geometry')
        if body_geometry is None:
            entity.pop('body_geometry', None)
        elif isinstance(body_geometry, dict):
            # Per-kind parameters are mutually exclusive; drop unused null keys
            # so each kind serializes only the fields it actually carries.
            for key in ('radius_m', 'footprint_vertices', 'mesh', 'mesh_reference'):
                if body_geometry.get(key) is None:
                    body_geometry.pop(key, None)
            if body_geometry.get('mesh_reference') is not None:
                # Issue #653: a referenced mesh body serializes only the compact
                # reference; ``mesh`` is an in-memory resolution cache. Optional
                # subfields stay canonical as well.
                body_geometry.pop('mesh', None)
                for container_key in ('mesh_reference',):
                    container = body_geometry.get(container_key)
                    if isinstance(container, dict) and container.get('import_authority') is None:
                        container.pop('import_authority', None)
            inline_mesh = body_geometry.get('mesh')
            if isinstance(inline_mesh, dict) and inline_mesh.get('import_authority') is None:
                inline_mesh.pop('import_authority', None)
    # Preserve N05/N10/N20 rectangular-room hashes by omitting the new optional field.
    if isinstance(payload.get('room'), dict) and payload['room'].get('footprint_vertices') is None:
        payload['room'].pop('footprint_vertices', None)
    # Preserve N05-N30a hashes until a wall topology is explicitly created.
    if payload.get('wall_topology') is None:
        payload.pop('wall_topology', None)
    # Preserve all pre-R120B hashes until semantic acoustic geometry is explicitly bound.
    if payload.get('r120_semantic_geometry') is None:
        payload.pop('r120_semantic_geometry', None)
    # Issue #661/#657: optional document-level records; omission preserves
    # pre-v5 scene hashes.
    if payload.get('attachments') is None:
        payload.pop('attachments', None)
    if payload.get('construction_assemblies') is None:
        payload.pop('construction_assemblies', None)
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


# F1_DOCUMENT_ID is the historical default document identity kept only for
# backward compatibility with existing data directories and as a test fixture
# identifier. Its value must never implicitly select synthetic content: the
# normal repository/Room path opens an unknown document — this one included —
# as an empty scene (#627). The synthetic development demo uses the separate
# SYNTHETIC_DEMO_DOCUMENT_ID identity in cad_synthetic_demo.py.
F1_DOCUMENT_ID = 'fixture-f1'


def make_empty_scene(document_id: str) -> SceneDocument:
    """The honest initial state of a fresh project: no room, no entities.

    An empty scene still reports truthful readiness (room/system undefined)
    instead of appearing complete through borrowed fixture geometry.
    """

    return SceneDocument(document_id=document_id, schema_version=2, room=None, entities=())


def make_f1_scene() -> SceneDocument:
    """The legacy F1 development fixture scene.

    Test/backward-compatibility helper only. Production startup must not call
    this implicitly for a missing document — callers that want this content
    (tests, fixtures, migrations) persist it explicitly.
    """

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