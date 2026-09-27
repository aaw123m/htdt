"""Speaker mounting/port installation context authority (#540).

An ``EquipmentDefinition`` declares supported mounting modes, clearance
requirements and port orientation; a ``SpeakerInstallationContext`` records how
one bound scene entity is actually installed (selected mounting mode, host
surface, measured clearances, explicit project overrides and the applicability
class of any directivity data). The context is a separate authority record —
it never mutates the frozen definition or the scene entity.

``evaluate_installation_context`` checks mounting-mode compatibility and
equipment-derived clearance requirements against the persisted scene geometry
and emits exact PASS/FAIL/UNKNOWN results. ``installation_constraint_results``
projects those results into ``CadConstraintResult`` items so they appear with
exact reasons inside the existing placement preflight/candidate rejection
channel.
"""

from __future__ import annotations

from math import isfinite, sqrt
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from shapely.geometry import LineString, Point, Polygon

from .cad_equipment import (
    ClearanceMetadata,
    EquipmentDataProvenance,
    EquipmentDefinition,
    MountingMetadata,
    PortMetadata,
)
from .cad_scene import (
    SceneDocument,
    SceneEntity,
    quaternion_to_matrix3,
)
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest


INSTALLATION_CONTEXT_SCHEMA_VERSION = 1
INSTALLATION_CONTEXT_AUTHORITY_VERSION = 'speaker-installation-context-1'
INSTALLATION_EVALUATION_VERSION = 'speaker-installation-evaluation-1'

MountingMode = Literal[
    'free_standing',
    'stand',
    'shelf',
    'wall',
    'ceiling',
    'in_wall',
    'in_ceiling',
    'custom',
    'unknown',
]
BaffleState = Literal[
    'free_space',
    'flush_baffle',
    'finite_baffle',
    'boundary_adjacent',
    'unknown',
]
DirectivityApplicability = Literal[
    'anechoic',
    'iec_baffle',
    'in_wall',
    'manufacturer_fixture',
    'unknown',
]
ClearanceAxis = Literal['front', 'rear', 'side', 'top', 'bottom', 'port']
InstallationCheckState = Literal['PASS', 'FAIL', 'UNKNOWN']
AcousticMountingCapability = Literal[
    'modeled_supported',
    'context_known_geometry_checked',
    'acoustic_effect_unsupported',
]
# #967: shared typed vocabulary for the physical installation condition a
# source is measured/operated under. Response and directivity evidence
# declares the condition it is valid for; the installation context derives
# the actual installed condition; R110 compares them explicitly instead of
# trusting a free-form label.
SourceInstallationCondition = Literal[
    'free_standing',
    'half_space_baffle',
    'flush_in_wall',
    'boundary_adjacent',
    'manufacturer_fixture',
    'unknown',
]
InstallationConditionCompatibility = Literal[
    'exact_match',
    'compatible_by_declared_model',
    'bounded_approximation',
    'incompatible',
    'unknown',
]

PORT_FACE: dict[str, tuple[ClearanceAxis, ...]] = {
    'sealed': (),
    'front': ('front',),
    'rear': ('rear',),
    'side': ('side',),
    'down': ('bottom',),
    'passive_radiator': ('rear',),
    'other': (),
    'unknown': (),
}






def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _finite(value: float, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


class MeasuredClearances(BaseModel):
    """Explicit measured/as-built clearance evidence for an installation."""

    model_config = ConfigDict(frozen=True)

    front_m: float | None = Field(default=None, ge=0.0)
    rear_m: float | None = Field(default=None, ge=0.0)
    side_m: float | None = Field(default=None, ge=0.0)
    top_m: float | None = Field(default=None, ge=0.0)
    bottom_m: float | None = Field(default=None, ge=0.0)
    port_to_boundary_m: float | None = Field(default=None, ge=0.0)

    @field_validator(
        'front_m',
        'rear_m',
        'side_m',
        'top_m',
        'bottom_m',
        'port_to_boundary_m',
    )
    @classmethod
    def finite_clearance(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='measured clearance')


class ClearanceOverride(BaseModel):
    """Explicit project override of an equipment clearance requirement."""

    model_config = ConfigDict(frozen=True)

    axis: ClearanceAxis
    required_m: float = Field(ge=0.0)
    rationale: str = Field(min_length=1)
    provenance: EquipmentDataProvenance

    @field_validator('required_m')
    @classmethod
    def finite_required(cls, value: float) -> float:
        return _finite(value, field_name='clearance override')


class InstallationEquipmentRef(BaseModel):
    """Exact EquipmentDefinition binding for an installation context."""

    model_config = ConfigDict(frozen=True)

    equipment_definition_id: str = Field(min_length=1)
    equipment_definition_version: str = Field(min_length=1)
    equipment_definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @classmethod
    def from_definition(
        cls,
        definition: EquipmentDefinition,
    ) -> 'InstallationEquipmentRef':
        return cls(
            equipment_definition_id=definition.definition_id,
            equipment_definition_version=definition.version,
            equipment_definition_sha256=definition.semantic_sha256,
        )


class SpeakerInstallationContext(BaseModel):
    """How one equipment-bound speaker entity is actually installed."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = INSTALLATION_CONTEXT_SCHEMA_VERSION
    authority_version: Literal[
        'speaker-installation-context-1'
    ] = INSTALLATION_CONTEXT_AUTHORITY_VERSION
    context_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    equipment: InstallationEquipmentRef
    selected_mounting_mode: MountingMode
    host_entity_id: str | None = Field(default=None, min_length=1)
    host_surface_label: str | None = Field(default=None, min_length=1)
    baffle_state: BaffleState = 'unknown'
    measured_clearances: MeasuredClearances | None = None
    clearance_overrides: tuple[ClearanceOverride, ...] = ()
    directivity_applicability: DirectivityApplicability = 'unknown'
    provenance: tuple[EquipmentDataProvenance, ...] = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_context(self) -> 'SpeakerInstallationContext':
        override_axes = [item.axis for item in self.clearance_overrides]
        if len(override_axes) != len(set(override_axes)):
            raise ValueError('clearance overrides must be unique per axis')
        if self.host_entity_id == self.entity_id:
            raise ValueError('installation host cannot be the entity itself')
        if self.semantic_sha256 != _digest(self.semantic_payload()):
            raise ValueError('SpeakerInstallationContext semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'context_id': self.context_id,
            'document_id': self.document_id,
            'entity_id': self.entity_id,
            'equipment': self.equipment.model_dump(mode='json'),
            'selected_mounting_mode': self.selected_mounting_mode,
            'host_entity_id': self.host_entity_id,
            'host_surface_label': self.host_surface_label,
            'baffle_state': self.baffle_state,
            'measured_clearances': (
                None
                if self.measured_clearances is None
                else self.measured_clearances.model_dump(mode='json')
            ),
            'clearance_overrides': [
                item.model_dump(mode='json')
                for item in self.clearance_overrides
            ],
            'directivity_applicability': self.directivity_applicability,
            'provenance': [item.model_dump(mode='json') for item in self.provenance],
            'created_at_utc': self.created_at_utc,
        }

    def override_for(self, axis: ClearanceAxis) -> ClearanceOverride | None:
        for item in self.clearance_overrides:
            if item.axis == axis:
                return item
        return None


def build_installation_context(
    *,
    context_id: str,
    document_id: str,
    entity_id: str,
    equipment_definition: EquipmentDefinition,
    selected_mounting_mode: MountingMode,
    provenance: Sequence[EquipmentDataProvenance],
    created_at_utc: str,
    host_entity_id: str | None = None,
    host_surface_label: str | None = None,
    baffle_state: BaffleState = 'unknown',
    measured_clearances: MeasuredClearances | None = None,
    clearance_overrides: Sequence[ClearanceOverride] = (),
    directivity_applicability: DirectivityApplicability = 'unknown',
) -> SpeakerInstallationContext:
    provenance_items = tuple(provenance)
    override_items = tuple(clearance_overrides)
    equipment = InstallationEquipmentRef.from_definition(equipment_definition)
    payload = {
        'schema_version': INSTALLATION_CONTEXT_SCHEMA_VERSION,
        'authority_version': INSTALLATION_CONTEXT_AUTHORITY_VERSION,
        'context_id': context_id,
        'document_id': document_id,
        'entity_id': entity_id,
        'equipment': equipment.model_dump(mode='json'),
        'selected_mounting_mode': selected_mounting_mode,
        'host_entity_id': host_entity_id,
        'host_surface_label': host_surface_label,
        'baffle_state': baffle_state,
        'measured_clearances': (
            None
            if measured_clearances is None
            else measured_clearances.model_dump(mode='json')
        ),
        'clearance_overrides': [
            item.model_dump(mode='json') for item in override_items
        ],
        'directivity_applicability': directivity_applicability,
        'provenance': [item.model_dump(mode='json') for item in provenance_items],
        'created_at_utc': created_at_utc,
    }
    return SpeakerInstallationContext(
        context_id=context_id,
        document_id=document_id,
        entity_id=entity_id,
        equipment=equipment,
        selected_mounting_mode=selected_mounting_mode,
        host_entity_id=host_entity_id,
        host_surface_label=host_surface_label,
        baffle_state=baffle_state,
        measured_clearances=measured_clearances,
        clearance_overrides=override_items,
        directivity_applicability=directivity_applicability,
        provenance=provenance_items,
        created_at_utc=created_at_utc,
        semantic_sha256=_digest(payload),
    )


class InstallationCheck(BaseModel):
    """One named installation check with exact PASS/FAIL/UNKNOWN semantics."""

    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    state: InstallationCheckState
    actual_m: float | None = None
    required_m: float | None = None
    reason: str = Field(min_length=1)

    @field_validator('actual_m', 'required_m')
    @classmethod
    def finite_value(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='installation check value')


def installed_condition(
    context: SpeakerInstallationContext,
) -> SourceInstallationCondition:
    """Derive the actual installed condition from mounting/baffle fields.

    An explicit ``baffle_state`` is the more specific declaration and wins
    over the coarse mounting mode; a declared pair that disagrees cannot
    certify a condition and resolves to ``unknown``.
    """

    baffle_map: dict[str, SourceInstallationCondition] = {
        'free_space': 'free_standing',
        'flush_baffle': 'flush_in_wall',
        'finite_baffle': 'half_space_baffle',
        'boundary_adjacent': 'boundary_adjacent',
    }
    mode_map: dict[str, SourceInstallationCondition] = {
        'free_standing': 'free_standing',
        'stand': 'free_standing',
        'shelf': 'boundary_adjacent',
        'wall': 'boundary_adjacent',
        'ceiling': 'boundary_adjacent',
        'in_wall': 'flush_in_wall',
        'in_ceiling': 'flush_in_wall',
    }
    baffle = baffle_map.get(context.baffle_state)
    mode = mode_map.get(context.selected_mounting_mode)
    if baffle is not None:
        if mode is not None and mode != baffle:
            return 'unknown'
        return baffle
    return mode if mode is not None else 'unknown'


def directivity_evidence_condition(
    context: SpeakerInstallationContext,
) -> SourceInstallationCondition:
    """Map the declared directivity applicability onto the condition axis."""

    return {
        'anechoic': 'free_standing',
        'iec_baffle': 'half_space_baffle',
        'in_wall': 'flush_in_wall',
        'manufacturer_fixture': 'manufacturer_fixture',
        'unknown': 'unknown',
    }[context.directivity_applicability]


_CONDITION_COMPATIBLE_DECLARED: frozenset[tuple[str, str]] = frozenset(
    {
        # An IEC/infinite-baffle measurement is physically the same
        # condition as a flush in-wall/in-ceiling installation.
        ('flush_in_wall', 'half_space_baffle'),
        ('half_space_baffle', 'flush_in_wall'),
    }
)
_CONDITION_BOUNDED_APPROXIMATION: frozenset[tuple[str, str]] = frozenset(
    {
        ('boundary_adjacent', 'free_standing'),
        ('boundary_adjacent', 'half_space_baffle'),
        ('half_space_baffle', 'boundary_adjacent'),
    }
)


def evaluate_installation_condition(
    installed: SourceInstallationCondition,
    evidence: SourceInstallationCondition,
) -> tuple[InstallationConditionCompatibility, str]:
    """Compare the actual installed condition with an evidence condition.

    Conservative and symmetric in failure only: only declared physical
    equivalences upgrade an exact match. Unknown on either side never
    produces a positive compatibility claim, and manufacturer-fixture
    evidence is meaningless outside its exact declared fixture.
    """

    if installed == 'unknown' or evidence == 'unknown':
        return 'unknown', 'installed or evidence condition is unrecorded'
    if installed == evidence:
        return (
            'exact_match',
            f'evidence condition {evidence} matches the installed condition',
        )
    if 'manufacturer_fixture' in (installed, evidence):
        return (
            'unknown',
            'manufacturer fixture conditions are only comparable to the exact '
            'same fixture authority',
        )
    pair = (installed, evidence)
    if pair in _CONDITION_COMPATIBLE_DECLARED:
        return (
            'compatible_by_declared_model',
            f'evidence condition {evidence} is the declared model of the '
            f'installed condition {installed}',
        )
    if pair in _CONDITION_BOUNDED_APPROXIMATION:
        return (
            'bounded_approximation',
            f'evidence condition {evidence} is only a bounded approximation '
            f'of the installed condition {installed}',
        )
    return (
        'incompatible',
        f'evidence condition {evidence} cannot serve the installed '
        f'condition {installed}',
    )


class InstallationEvaluation(BaseModel):
    """Immutable evaluation of one persisted installation context."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = INSTALLATION_CONTEXT_SCHEMA_VERSION
    authority_version: Literal[
        'speaker-installation-evaluation-1'
    ] = INSTALLATION_EVALUATION_VERSION
    context_id: str = Field(min_length=1)
    context_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    entity_id: str = Field(min_length=1)
    checks: tuple[InstallationCheck, ...]
    mounting_compatible: bool | None
    installed_condition: SourceInstallationCondition
    evidence_condition: SourceInstallationCondition
    condition_compatibility: InstallationConditionCompatibility
    acoustic_mounting_capability: AcousticMountingCapability
    evaluation_id: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_identity(self) -> 'InstallationEvaluation':
        digest = _digest(self.identity_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError('InstallationEvaluation semantic hash mismatch')
        if self.evaluation_id != _semantic_id('install-eval', digest):
            raise ValueError(
                'InstallationEvaluation ID does not match semantic hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'context_id': self.context_id,
            'context_sha256': self.context_sha256,
            'entity_id': self.entity_id,
            'checks': [check.model_dump(mode='json') for check in self.checks],
            'mounting_compatible': self.mounting_compatible,
            'installed_condition': self.installed_condition,
            'evidence_condition': self.evidence_condition,
            'condition_compatibility': self.condition_compatibility,
            'acoustic_mounting_capability': self.acoustic_mounting_capability,
        }

    @property
    def failed_checks(self) -> tuple[InstallationCheck, ...]:
        return tuple(check for check in self.checks if check.state == 'FAIL')


def _room_polygon(document: SceneDocument) -> Polygon | None:
    room = document.room
    if room is None:
        return None
    vertices = room.footprint_vertices
    if vertices:
        polygon = Polygon([(v.x_m, v.y_m) for v in vertices])
    else:
        polygon = Polygon(
            [(0.0, 0.0), (room.width_m, 0.0), (room.width_m, room.depth_m), (0.0, room.depth_m)]
        )
    return polygon if polygon.is_valid and not polygon.is_empty else None


def _entity_polygon(entity: SceneEntity) -> Polygon | None:
    """World-frame entity footprint polygon, or None when unresolvable."""
    if entity.size_m is None and entity.body_geometry is None:
        return None
    if entity.body_geometry is not None:
        footprint = entity.body_geometry.footprint_vertices
        if footprint is not None:
            local = [(v.x_m, v.y_m) for v in footprint]
        elif entity.size_m is not None:
            hx, hy = entity.size_m.x_m * 0.5, entity.size_m.y_m * 0.5
            local = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]
        else:
            return None
    else:
        assert entity.size_m is not None
        hx, hy = entity.size_m.x_m * 0.5, entity.size_m.y_m * 0.5
        local = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]

    rotation = quaternion_to_matrix3(entity.orientation)
    world: list[tuple[float, float]] = []
    for x, y in local:
        wx = rotation[0][0] * x + rotation[0][1] * y
        wy = rotation[1][0] * x + rotation[1][1] * y
        world.append((entity.position.x_m + wx, entity.position.y_m + wy))
    polygon = Polygon(world)
    return polygon if polygon.is_valid and not polygon.is_empty else None


def _axis_gap(
    entity_polygon: Polygon,
    room_polygon: Polygon,
    direction: tuple[float, float],
) -> float | None:
    """Distance the entity can translate along *direction* before exiting.

    Computed as the minimum positive ray distance from any entity-footprint
    vertex to the room boundary along the direction.
    """
    dx, dy = direction
    span = (
        abs(room_polygon.bounds[2] - room_polygon.bounds[0])
        + abs(room_polygon.bounds[3] - room_polygon.bounds[1])
    ) * 2.0 + 1.0
    boundary = room_polygon.boundary
    best: float | None = None
    for vertex in entity_polygon.exterior.coords:
        ray = LineString([vertex, (vertex[0] + dx * span, vertex[1] + dy * span)])
        hit = ray.intersection(boundary)
        if hit.is_empty:
            continue
        distances: list[float] = []
        geometries = (
            list(hit.geoms) if hasattr(hit, 'geoms') else [hit]
        )
        origin = Point(vertex)
        for geom in geometries:
            if geom.geom_type == 'Point':
                distances.append(origin.distance(geom))
            elif geom.geom_type in ('LineString', 'MultiLineString'):
                distances.append(origin.distance(geom))
        for distance in distances:
            if distance > 1e-9 and (best is None or distance < best):
                best = distance
    return best


def _horizontal_projection(
    direction3: tuple[float, float, float],
) -> tuple[float, float] | None:
    """Unit XY projection of a world direction, None when near-vertical."""
    dx, dy = direction3[0], direction3[1]
    norm = sqrt(dx * dx + dy * dy)
    if norm <= 1e-6:
        return None
    return (dx / norm, dy / norm)


def _axis_actual_clearance(
    axis: ClearanceAxis,
    entity: SceneEntity,
    entity_polygon: Polygon | None,
    room_polygon: Polygon | None,
    document: SceneDocument,
) -> float | None:
    """Floor-plan gap along a cabinet-local axis of the entity's orientation.

    Front/rear/side/port clearances follow the cabinet: local +Y is the
    authored cabinet front, local -Y the rear and ±X the sides
    (:mod:`cad_orientation_display` shares the convention). Top/bottom stay
    world-vertical — they measure floor/ceiling clearance, not cabinet faces.
    A cabinet face that points near-vertical has no horizontal clearance
    direction and stays unresolvable (``None``) rather than guessing a wall.
    """
    if axis in ('top', 'bottom'):
        room = document.room
        if room is None or entity.size_m is None:
            return None
        half = entity.size_m.z_m * 0.5
        if axis == 'top':
            return room.height_m - (entity.position.z_m + half)
        return entity.position.z_m - half
    if entity_polygon is None or room_polygon is None:
        return None
    rotation = quaternion_to_matrix3(entity.orientation)
    # Column 1 of the rotation matrix is the world direction of local +Y
    # (cabinet front); column 0 is local +X (cabinet right side).
    front_direction = _horizontal_projection(
        (rotation[0][1], rotation[1][1], rotation[2][1])
    )
    right_direction = _horizontal_projection(
        (rotation[0][0], rotation[1][0], rotation[2][0])
    )
    if axis == 'front':
        if front_direction is None:
            return None
        return _axis_gap(entity_polygon, room_polygon, front_direction)
    if axis == 'rear':
        if front_direction is None:
            return None
        return _axis_gap(
            entity_polygon, room_polygon, (-front_direction[0], -front_direction[1])
        )
    if axis == 'side':
        if right_direction is None:
            return None
        left = _axis_gap(
            entity_polygon,
            room_polygon,
            (-right_direction[0], -right_direction[1]),
        )
        right = _axis_gap(entity_polygon, room_polygon, right_direction)
        candidates = [gap for gap in (left, right) if gap is not None]
        return min(candidates) if candidates else None
    return None


def _required_clearance(
    axis: ClearanceAxis,
    clearance: ClearanceMetadata | None,
    port: PortMetadata | None,
) -> float | None:
    if clearance is not None:
        required = {
            'front': clearance.front_m,
            'rear': clearance.rear_m,
            'side': clearance.side_m,
            'top': clearance.top_m,
            'bottom': clearance.bottom_m,
        }.get(axis)
        if required is not None:
            return float(required)
    if port is not None and port.minimum_clearance_m is not None:
        if axis in PORT_FACE.get(port.port_type, ()):
            return float(port.minimum_clearance_m)
    return None


def evaluate_installation_context(
    *,
    document: SceneDocument,
    entity: SceneEntity,
    equipment_definition: EquipmentDefinition,
    context: SpeakerInstallationContext,
) -> InstallationEvaluation:
    """Check mounting/port/clearance authority against scene geometry."""
    if context.document_id != document.document_id:
        raise ValueError('installation context document mismatch')
    if context.entity_id != entity.entity_id:
        raise ValueError('installation context entity mismatch')
    if context.entity_id not in {item.entity_id for item in document.entities}:
        raise ValueError('installation context entity is not in the scene')
    if entity.kind != 'speaker':
        raise ValueError('installation context requires a speaker entity')
    if (
        context.equipment.equipment_definition_id
        != equipment_definition.definition_id
        or context.equipment.equipment_definition_version
        != equipment_definition.version
        or context.equipment.equipment_definition_sha256
        != equipment_definition.semantic_sha256
    ):
        raise ValueError(
            'installation context does not reference the supplied '
            'EquipmentDefinition exactly'
        )

    checks: list[InstallationCheck] = []
    mounting = equipment_definition.mounting
    if context.selected_mounting_mode == 'unknown':
        checks.append(
            InstallationCheck(
                check='mounting_mode',
                state='UNKNOWN',
                reason='selected mounting mode is unrecorded',
            )
        )
        mounting_compatible: bool | None = None
    elif (
        mounting is not None
        and mounting.mounting_modes
        and context.selected_mounting_mode not in mounting.mounting_modes
    ):
        checks.append(
            InstallationCheck(
                check='mounting_mode',
                state='FAIL',
                reason=(
                    f'selected mounting mode {context.selected_mounting_mode} '
                    'is not supported by the equipment definition'
                ),
            )
        )
        mounting_compatible = False
    elif mounting is not None and mounting.mounting_modes:
        checks.append(
            InstallationCheck(
                check='mounting_mode',
                state='PASS',
                reason=(
                    f'selected mounting mode {context.selected_mounting_mode} '
                    'is supported by the equipment definition'
                ),
            )
        )
        mounting_compatible = True
    else:
        checks.append(
            InstallationCheck(
                check='mounting_mode',
                state='UNKNOWN',
                reason='equipment definition declares no mounting modes',
            )
        )
        mounting_compatible = None

    entity_polygon = _entity_polygon(entity)
    room_polygon = _room_polygon(document)
    measured = context.measured_clearances
    axes: tuple[ClearanceAxis, ...] = (
        'front',
        'rear',
        'side',
        'top',
        'bottom',
    )
    for axis in axes:
        override = context.override_for(axis)
        required = (
            float(override.required_m)
            if override is not None
            else _required_clearance(
                axis, equipment_definition.clearance, equipment_definition.port
            )
        )
        if required is None:
            continue
        actual = getattr(measured, f'{axis}_m', None) if measured is not None else None
        if actual is None:
            actual = _axis_actual_clearance(
                axis, entity, entity_polygon, room_polygon, document
            )
        if actual is None:
            checks.append(
                InstallationCheck(
                    check=f'clearance_{axis}',
                    state='UNKNOWN',
                    required_m=required,
                    reason='clearance is required but no scene or measured '
                    'evidence resolves it',
                )
            )
            continue
        passed = actual >= required - 1e-9
        checks.append(
            InstallationCheck(
                check=f'clearance_{axis}',
                state='PASS' if passed else 'FAIL',
                actual_m=actual,
                required_m=required,
                reason=(
                    f'{axis} clearance {actual:.3f} m '
                    f'{"satisfies" if passed else "is below"} the required '
                    f'{required:.3f} m'
                    + (' (project override)' if override is not None else '')
                ),
            )
        )

    port = equipment_definition.port
    port_override = context.override_for('port')
    if port is not None and port.minimum_clearance_m is not None:
        port_axes = PORT_FACE.get(port.port_type, ())
        required = (
            float(port_override.required_m)
            if port_override is not None
            else float(port.minimum_clearance_m)
        )
        if not port_axes:
            checks.append(
                InstallationCheck(
                    check='port_clearance',
                    state='UNKNOWN',
                    required_m=required,
                    reason=(
                        f'port type {port.port_type} has no resolvable '
                        'boundary direction'
                    ),
                )
            )
        else:
            actual = (
                measured.port_to_boundary_m
                if measured is not None and measured.port_to_boundary_m is not None
                else None
            )
            if actual is None:
                face_values = [
                    _axis_actual_clearance(
                        face, entity, entity_polygon, room_polygon, document
                    )
                    for face in port_axes
                ]
                face_values = [v for v in face_values if v is not None]
                actual = min(face_values) if face_values else None
            if actual is None:
                checks.append(
                    InstallationCheck(
                        check='port_clearance',
                        state='UNKNOWN',
                        required_m=required,
                        reason='port clearance is required but unresolvable '
                        'from scene or measured evidence',
                    )
                )
            else:
                passed = actual >= required - 1e-9
                checks.append(
                    InstallationCheck(
                        check='port_clearance',
                        state='PASS' if passed else 'FAIL',
                        actual_m=actual,
                        required_m=required,
                        reason=(
                            f'port clearance {actual:.3f} m '
                            f'{"satisfies" if passed else "is below"} the '
                            f'required {required:.3f} m'
                            + (
                                ' (project override)'
                                if port_override is not None
                                else ''
                            )
                        ),
                    )
                )

    failed = any(check.state == 'FAIL' for check in checks)
    unknown = any(check.state == 'UNKNOWN' for check in checks)
    installed = installed_condition(context)
    evidence = directivity_evidence_condition(context)
    compatibility, _compatibility_reason = evaluate_installation_condition(
        installed, evidence
    )
    if failed or mounting_compatible is False:
        capability: AcousticMountingCapability = 'acoustic_effect_unsupported'
    elif compatibility in (
        'exact_match',
        'compatible_by_declared_model',
    ) and not unknown:
        capability = 'modeled_supported'
    elif checks:
        capability = 'context_known_geometry_checked'
    else:
        capability = 'acoustic_effect_unsupported'

    payload = {
        'schema_version': INSTALLATION_CONTEXT_SCHEMA_VERSION,
        'authority_version': INSTALLATION_EVALUATION_VERSION,
        'context_id': context.context_id,
        'context_sha256': context.semantic_sha256,
        'entity_id': entity.entity_id,
        'checks': [check.model_dump(mode='json') for check in checks],
        'mounting_compatible': mounting_compatible,
        'installed_condition': installed,
        'evidence_condition': evidence,
        'condition_compatibility': compatibility,
        'acoustic_mounting_capability': capability,
    }
    digest = _digest(payload)
    return InstallationEvaluation(
        context_id=context.context_id,
        context_sha256=context.semantic_sha256,
        entity_id=entity.entity_id,
        checks=tuple(checks),
        mounting_compatible=mounting_compatible,
        installed_condition=installed,
        evidence_condition=evidence,
        condition_compatibility=compatibility,
        acoustic_mounting_capability=capability,
        evaluation_id=_semantic_id('install-eval', digest),
        evaluation_sha256=digest,
    )


def installation_constraint_results(
    evaluation: InstallationEvaluation,
) -> tuple['CadConstraintResult', ...]:
    """Project installation checks into the placement-preflight channel.

    Each check becomes a ``CadConstraintResult`` of kind
    ``equipment_installation`` so candidate rejection and placement preflight
    surfaces render the exact reason codes without a second model.
    """
    from .cad_constraint_models import CadConstraintResult

    reason_codes = {
        'PASS': 'equipment_installation.passed',
        'FAIL': 'equipment_installation.failed',
        'UNKNOWN': 'equipment_installation.unknown',
    }
    results: list[CadConstraintResult] = []
    for check in evaluation.checks:
        results.append(
            CadConstraintResult(
                result_id=(
                    f'__installation__:{evaluation.context_id}:'
                    f'{check.check}'
                ),
                constraint_id='__installation__',
                kind='equipment_installation',
                name=f'機器設置条件 ({check.check})',
                entity_ids=(evaluation.entity_id,),
                passed=check.state == 'PASS',
                reason_code=reason_codes[check.state],
                reason_ja=check.reason,
                actual_m=check.actual_m,
                required_min_m=check.required_m,
            )
        )
    return tuple(results)


def installation_preflight_violations(
    evaluation: InstallationEvaluation,
) -> tuple[str, ...]:
    """Exact FAIL reasons for placement/candidate preflight surfaces."""
    return tuple(
        check.reason
        for check in evaluation.checks
        if check.state == 'FAIL'
    )


__all__ = [
    'AcousticMountingCapability',
    'BaffleState',
    'ClearanceAxis',
    'ClearanceOverride',
    'DirectivityApplicability',
    'INSTALLATION_CONTEXT_AUTHORITY_VERSION',
    'INSTALLATION_CONTEXT_SCHEMA_VERSION',
    'INSTALLATION_EVALUATION_VERSION',
    'InstallationCheck',
    'InstallationCheckState',
    'InstallationEquipmentRef',
    'InstallationEvaluation',
    'MeasuredClearances',
    'MountingMode',
    'SpeakerInstallationContext',
    'build_installation_context',
    'evaluate_installation_context',
    'installation_constraint_results',
    'installation_preflight_violations',
]
