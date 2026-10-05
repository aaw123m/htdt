"""openBIM / IFC 4.3 interoperability authority (issue #578).

A provenance-safe exchange layer between building BIM models and HTDT.
IFC is the *external* authority for architecture; HTDT stays the
authority for acoustic evidence. This module never fabricates what the
file did not declare:

- :class:`IfcImportArtifact` — sealed provenance record for one imported
  file: raw file identity + SHA-256, schema identifier, importer and
  source-application identity, declared units, coordinate/reference
  declarations, spatial hierarchy, mapping/coverage manifest and
  warnings. (issue #578 §1)
- :class:`IfcCoordinateAuthority` — pinned coordinate declarations:
  length/angle units + scale to metres, true-north vector, georeference
  (IfcMapConversion) state, axis convention, WCS origin and the origin
  shift actually applied. Nothing is guessed — an undeclared unit is
  recorded as ``undeclared`` and normalized coordinates are withheld,
  never assumed. (issue #578 §2)
- :class:`IfcEntityMapping` — per-entity mapping record: external id
  (GlobalId), semantic + geometry fingerprints, parent/storey context,
  resolved world transform in both source units and metres, mapped
  properties, material build-up layers, openings and reconciliation
  state. (issue #578 §3, §4, §7)
- :class:`IfcRevisionDelta` — diff of a newer source revision against
  the prior import: unchanged / geometry-changed / semantics-changed /
  new / missing / ambiguous entities, each flagged for acoustic
  relevance. Ambiguous rebinds require explicit reconciliation; nothing
  silently re-attaches to a recreated object. (issue #578 §5-§6)
- :class:`IfcIntakeProfile` + :class:`IfcIntakeEvaluation` — an
  IDS-equivalent (IDS 1.0, bSI 2024-06-01) machine-checkable intake
  contract: requirements such as valid schema, declared units, at least
  one space, storey context, geometric representation, opening identity,
  material build-up. Failures produce actionable missing-data
  diagnostics, not generic parser errors. (issue #578 §8)
- :class:`IfcExportPackage` — bounded create-new export in one of the
  declared modes REFERENCE_EXPORT / UPDATE_PROPOSAL / AS_BUILT_HANDOFF;
  a source model is never mutated and sections that cannot be exported
  are listed honestly. (issue #578 §9, §10)

Contract properties:

- coordinate fidelity is fail-closed: no silent mm<->m ambiguity, no
  silent axis reflection, original and normalized coordinates both
  inspectable (issue #578 §2);
- IFC construction material/build-up becomes evidence *candidates* —
  acoustic absorption stays UNKNOWN without external evidence (§4);
- external ids are never trusted without semantic reconciliation (§7);
- unmapped entities, unread geometry and lost attributes are reported
  honestly in the coverage record, not dropped (§1);
- no customer BIM bytes are sent anywhere; everything runs locally (§13).

References: ISO 16739-1:2024 (IFC 4.3.2.0 / IFC4X3_ADD2), ISO 10303-21,
bSI "User Guide for Geo-referencing in IFC", bSI IDS 1.0 (2024-06-01).
"""

from __future__ import annotations

import re
from hashlib import sha256
from math import atan2, isfinite, pi
from typing import Any, Literal, Mapping, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .canonical_json import (
    canonical_sha256 as _digest,
    canonicalize_payload as _canon,
)
from .ifc_step import (
    IfcStepEntity,
    IfcStepEnum,
    IfcStepHeader,
    IfcStepOmitted,
    IfcStepParseError,
    IfcStepRef,
    IfcStepTypedValue,
    parse_ifc_step,
)


_SHA256_PATTERN = r'^[0-9a-f]{64}$'

IFC_INTEROP_SCHEMA_VERSION = 1
IFC_INTEROP_AUTHORITY_VERSION = 'ifc-interop-1'

#: Schema identifiers this reader supports semantically. Other identifiers
#: are preserved verbatim in the artifact but resolution stops — never a
#: best-guess parse under a foreign schema.
SUPPORTED_IFC_SCHEMA_IDENTIFIERS = (
    'IFC4X3_ADD2',
    'IFC4X3_ADD1',
    'IFC4X3',
    'IFC4',
)

#: IFC axis convention is fixed by the schema: right-handed Cartesian,
#: Z-up. Recorded as a declaration, never an assumption to re-derive.
IFC_AXIS_CONVENTION = 'right_handed_z_up'

#: SI unit prefix scale factors (IfcSIPrefix).
_SI_PREFIX_SCALE = {
    'EXA': 1e18, 'PETA': 1e15, 'TERA': 1e12, 'GIGA': 1e9, 'MEGA': 1e6,
    'KILO': 1e3, 'HECTO': 1e2, 'DECA': 1e1,
    'DECI': 1e-1, 'CENTI': 1e-2, 'MILLI': 1e-3, 'MICRO': 1e-6,
    'NANO': 1e-9, 'PICO': 1e-12, 'FEMTO': 1e-15, 'ATTO': 1e-18,
}
_SI_UNIT_BASE = {
    'METRE': ('metre', 1.0),
    'RADIAN': ('radian', 1.0),
    'STERADIAN': ('steradian', 1.0),
    'SECOND': ('second', 1.0),
    'KILOGRAM': ('kilogram', 1.0),
    'SQUARE_METRE': ('square_metre', 1.0),
    'CUBIC_METRE': ('cubic_metre', 1.0),
}

#: Semantic role map (issue #578 §3). Entities outside this map are
#: preserved in coverage counts but do not fabricate HTDT roles.
IFC_SEMANTIC_ROLES = {
    'IFCSPACE': 'room_candidate',
    'IFCWALL': 'boundary',
    'IFCWALLSTANDARDCASE': 'boundary',
    'IFCSLAB': 'boundary',
    'IFCROOF': 'boundary',
    'IFCPLATE': 'boundary',
    'IFCCURTAINWALL': 'boundary',
    'IFCDOOR': 'opening',
    'IFCWINDOW': 'opening',
    'IFCOPENINGELEMENT': 'opening',
    'IFCCOVERING': 'treatment_candidate',
    'IFCFURNISHINGELEMENT': 'furniture',
    'IFCBUILDINGELEMENTPROXY': 'generic',
    'IFCCOLUMN': 'structure',
    'IFCBEAM': 'structure',
    'IFCMEMBER': 'structure',
    'IFCSTAIR': 'structure',
    'IFCSTAIRFLIGHT': 'structure',
    'IFCRAMP': 'structure',
    'IFCRAILING': 'generic',
}

SpatialKind = Literal['site', 'building', 'storey', 'space']

IfcHtdtRole = Literal[
    'room_candidate',
    'boundary',
    'opening',
    'treatment_candidate',
    'furniture',
    'generic',
    'structure',
    'unmapped',
]

IfcReconciliationState = Literal[
    'new',
    'unchanged',
    'geometry_changed',
    'semantics_changed',
    'stale',
    'ambiguous',
]

IfcDeltaChangeKind = Literal[
    'unchanged',
    'geometry_changed',
    'semantics_changed',
    'added',
    'removed',
    'ambiguous_rebind',
]

IfcUnitState = Literal['declared', 'undeclared', 'ambiguous']

IfcGeorefState = Literal['declared', 'not_declared']

IfcIntakeVerdict = Literal['satisfied', 'unsatisfied', 'not_applicable']

IfcIntakeOverall = Literal[
    'satisfied',
    'satisfied_with_gaps',
    'failed',
]

IfcExportMode = Literal[
    'reference_export',
    'update_proposal',
    'as_built_handoff',
]


class IfcInteropError(ValueError):
    """Structural IFC failure that must abort the import."""


# ---------------------------------------------------------------------------
# Linear algebra (small, dependency-free)
# ---------------------------------------------------------------------------

Vec3 = tuple[float, float, float]
Mat4 = tuple[tuple[float, float, float, float], ...]


def _vec(values: Sequence[float]) -> Vec3:
    v = tuple(float(x) for x in values)
    if len(v) != 3:
        raise IfcInteropError('expected a 3-component vector')
    for x in v:
        if not isfinite(x):
            raise IfcInteropError('non-finite vector component')
    return v  # type: ignore[return-value]


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _norm(a: Vec3) -> Vec3:
    n = _dot(a, a) ** 0.5
    if n <= 0.0:
        raise IfcInteropError('zero-length direction vector')
    return (a[0] / n, a[1] / n, a[2] / n)


def _orthonormal_frame(
    axis: Vec3 | None, ref: Vec3 | None
) -> tuple[Vec3, Vec3, Vec3]:
    """Right-handed frame per IfcAxis2Placement3D rules.

    Axis is the local Z; RefDirection the local X (defaults (1,0,0) in
    the plane normal to Axis; default Axis (0,0,1)). Right-handed by
    schema — a degenerate input fails, it is never "fixed" silently.
    """
    z = _norm(axis) if axis is not None else (0.0, 0.0, 1.0)
    if ref is None:
        ref = (0.0, 0.0, 1.0) if abs(z[2]) < 0.5 else (1.0, 0.0, 0.0)
    r = _norm(ref)
    x = _sub(r, tuple(z[i] * _dot(r, z) for i in range(3)))
    if _dot(x, x) <= 1e-24:
        raise IfcInteropError('ref direction parallel to axis')
    x = _norm(x)
    y = _cross(z, x)
    return x, y, z


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _mat_from_frame(x: Vec3, y: Vec3, z: Vec3, t: Vec3) -> Mat4:
    return (
        (x[0], y[0], z[0], t[0]),
        (x[1], y[1], z[1], t[1]),
        (x[2], y[2], z[2], t[2]),
        (0.0, 0.0, 0.0, 1.0),
    )


def _mat_mul(a: Mat4, b: Mat4) -> Mat4:
    out = []
    for i in range(4):
        row = []
        for j in range(4):
            row.append(sum(a[i][k] * b[k][j] for k in range(4)))
        out.append(tuple(row))
    return tuple(out)


def _mat_scale_translation(m: Mat4, scale: float) -> Mat4:
    """Scale the translation column of a rigid transform to a new unit.

    Rotation blocks are dimensionless; only translations carry length
    units. Converting a source-unit world transform to metres never
    touches orientation.
    """
    return (
        (m[0][0], m[0][1], m[0][2], m[0][3] * scale),
        (m[1][0], m[1][1], m[1][2], m[1][3] * scale),
        (m[2][0], m[2][1], m[2][2], m[2][3] * scale),
        (m[3][0], m[3][1], m[3][2], m[3][3]),
    )


def _mat_flat(m: Mat4) -> tuple[float, ...]:
    return tuple(m[i][j] for i in range(4) for j in range(4))


def _require_finite_tuple(
    values: tuple[float, ...] | None, name: str
) -> None:
    if values is None:
        return
    for v in values:
        if not isfinite(v):
            raise ValueError(f'{name} must be finite')


# ---------------------------------------------------------------------------
# Resolved (non-sealed) intermediate model
# ---------------------------------------------------------------------------


class IfcMaterialLayerEvidence(BaseModel):
    """One declared material layer — construction evidence, never an
    acoustic coefficient (issue #578 §4)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    material_name: str | None = None
    layer_thickness_source_units: float | None = None
    layer_thickness_m: float | None = None
    is_ventilated: bool | None = None
    name: str | None = None


class IfcPropertyValue(BaseModel):
    """One IfcPropertySingleValue, raw-value preserving."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    name: str
    ifc_value_type: str
    value: Any = None
    unit_entity_id: int | None = None


class IfcOpeningLink(BaseModel):
    """Opening hosted in a boundary element (IfcRelVoidsElement) and its
    optional filling element (IfcRelFillsElement)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    opening_global_id: str | None = None
    opening_name: str | None = None
    filling_global_id: str | None = None
    filling_type: str | None = None


class IfcGeometryDescriptor(BaseModel):
    """Bounded extracted geometry for one representation item.

    Only a fixed set of representation classes is extracted
    (extruded-area solids, faceted breps, bounding boxes); anything else
    is counted in ``unresolved_representation_items`` instead of being
    approximated silently.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: Literal['extruded_area_solid', 'faceted_brep', 'bounding_box']
    identifier: str | None = None
    representation_type: str | None = None
    profile_name: str | None = None
    profile_points: tuple[tuple[float, float], ...] = ()
    extrusion_direction: tuple[float, float, float] | None = None
    depth_source_units: float | None = None
    vertices: tuple[tuple[float, float, float], ...] = ()
    bounds_min: tuple[float, float, float] | None = None
    bounds_max: tuple[float, float, float] | None = None
    fingerprint: str = Field(min_length=1)


class IfcCoordinateAuthority(BaseModel):
    """Pinned coordinate declarations for one import (issue #578 §2)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    length_unit_name: str | None = None
    length_unit_state: IfcUnitState = 'undeclared'
    length_scale_to_meter: float | None = None
    plane_angle_unit_name: str | None = None
    plane_angle_scale_to_radian: float | None = None
    axis_convention: str = IFC_AXIS_CONVENTION
    true_north_vector: tuple[float, float] | None = None
    true_north_state: Literal['declared', 'not_declared'] = 'not_declared'
    georef_state: IfcGeorefState = 'not_declared'
    georef_source_crs_context: str | None = None
    georef_target_crs_name: str | None = None
    georef_eastings: float | None = None
    georef_northings: float | None = None
    georef_orthogonal_height: float | None = None
    georef_x_axis_easting: float | None = None
    georef_x_axis_northing: float | None = None
    georef_scale: float | None = None
    wcs_origin_source_units: tuple[float, float, float] | None = None
    origin_shift_applied: tuple[float, float, float] | None = None

    @field_validator(
        'length_scale_to_meter',
        'plane_angle_scale_to_radian',
        'georef_eastings',
        'georef_northings',
        'georef_orthogonal_height',
        'georef_x_axis_easting',
        'georef_x_axis_northing',
        'georef_scale',
    )
    @classmethod
    def _finite(cls, value: float | None) -> float | None:
        if value is not None and not isfinite(value):
            raise ValueError('coordinate declaration must be finite')
        return value

    @model_validator(mode='after')
    def _consistency(self) -> 'IfcCoordinateAuthority':
        if self.length_unit_state == 'declared' and (
            self.length_scale_to_meter is None
        ):
            raise ValueError('declared length unit requires a metre scale')
        if self.length_unit_state == 'undeclared' and (
            self.length_scale_to_meter is not None
        ):
            raise ValueError(
                'undeclared length unit cannot carry a metre scale'
            )
        if self.true_north_state == 'declared' and (
            self.true_north_vector is None
        ):
            raise ValueError('declared true north requires its vector')
        return self

    @property
    def true_north_angle_deg(self) -> float | None:
        if self.true_north_vector is None:
            return None
        x, y = self.true_north_vector
        return atan2(y, x) * 180.0 / pi


class IfcImportCoverage(BaseModel):
    """Honest coverage report (issue #578 §1): what was read, what was
    mapped, and what was not understood."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    entity_total: int = 0
    mapped_entity_count: int = 0
    spatial_entity_count: int = 0
    unresolved_placement_count: int = 0
    unhandled_entity_types: dict[str, int] = Field(default_factory=dict)
    unresolved_representation_items: dict[str, int] = Field(
        default_factory=dict
    )
    dropped_property_count: int = 0
    relation_counts: dict[str, int] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()


class IfcResolvedElement(BaseModel):
    """One semantically mapped IFC entity (internal resolution result)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    step_entity_id: int
    ifc_type: str
    global_id: str | None
    name: str | None
    description: str | None
    htdt_role: IfcHtdtRole
    parent_global_id: str | None
    parent_kind: str | None
    world_transform_source_units: tuple[float, ...] | None
    """Resolved placement chain in source units; ``None`` when the
    placement could not be resolved — withheld, never faked (§2)."""
    world_transform_m: tuple[float, ...] | None
    placement_chain_ids: tuple[int, ...]
    properties: dict[str, IfcPropertyValue] = Field(default_factory=dict)
    material_layers: tuple[IfcMaterialLayerEvidence, ...] = ()
    openings: tuple[IfcOpeningLink, ...] = ()
    geometry_descriptors: tuple[IfcGeometryDescriptor, ...] = ()
    unresolved_representation_items: tuple[str, ...] = ()
    semantic_fingerprint: str = Field(min_length=1)
    geometry_fingerprint: str = Field(min_length=1)


class IfcResolvedSpatialNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    step_entity_id: int
    kind: SpatialKind
    global_id: str | None
    name: str | None
    parent_global_id: str | None
    elevation_source_units: float | None = None
    world_transform_source_units: tuple[float, ...] | None


class IfcResolvedModel(BaseModel):
    """Full semantic resolution of one STEP document."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_identifier: str
    schema_supported: bool
    source_application: str | None
    preprocessor_version: str | None
    source_timestamp: str | None
    source_file_name: str | None
    coordinate: IfcCoordinateAuthority
    project_global_id: str | None
    project_name: str | None
    spatial_nodes: tuple[IfcResolvedSpatialNode, ...]
    elements: tuple[IfcResolvedElement, ...]
    coverage: IfcImportCoverage


# ---------------------------------------------------------------------------
# STEP entity -> semantic resolution
# ---------------------------------------------------------------------------

_MAX_PROPERTIES_PER_ENTITY = 2000


def _arg_ref(value: Any) -> int | None:
    return value.entity_id if isinstance(value, IfcStepRef) else None


def _arg_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _arg_num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _arg_enum_name(value: Any) -> str | None:
    return value.name if isinstance(value, IfcStepEnum) else None


def _point_coords(entity: IfcStepEntity) -> tuple[float, ...]:
    if not entity.args:
        raise IfcInteropError(
            f'#{entity.entity_id}: {entity.name} missing coordinates'
        )
    coords = entity.args[0]
    if not isinstance(coords, tuple):
        raise IfcInteropError(
            f'#{entity.entity_id}: {entity.name} bad coordinate form'
        )
    out: list[float] = []
    for c in coords:
        if not isinstance(c, (int, float)):
            raise IfcInteropError(
                f'#{entity.entity_id}: {entity.name} bad coordinate value'
            )
        out.append(float(c))
    return tuple(out)


class _Index:
    """Entity and relationship index over the parsed STEP document."""

    def __init__(self, entities: Sequence[IfcStepEntity]) -> None:
        self.by_id: dict[int, IfcStepEntity] = {}
        self.by_type: dict[str, list[IfcStepEntity]] = {}
        for e in entities:
            self.by_id[e.entity_id] = e
            self.by_type.setdefault(e.name, []).append(e)

    def get(self, ref: Any) -> IfcStepEntity | None:
        if not isinstance(ref, IfcStepRef):
            return None
        return self.by_id.get(ref.entity_id)

    def require(self, ref: Any, what: str) -> IfcStepEntity:
        entity = self.get(ref)
        if entity is None:
            rid = ref.entity_id if isinstance(ref, IfcStepRef) else '?'
            raise IfcInteropError(
                f'unresolved entity reference #{rid} for {what}'
            )
        return entity

    def typed(self, *names: str) -> list[IfcStepEntity]:
        out: list[IfcStepEntity] = []
        for name in names:
            out.extend(self.by_type.get(name, ()))
        return out


def _axis_placement(index: _Index, ref: Any) -> Mat4:
    """IfcAxis2Placement2D/3D -> local-to-parent 4x4."""
    entity = index.require(ref, 'axis placement')
    if entity.name == 'IFCAXIS2PLACEMENT3D':
        loc = index.require(entity.args[0], 'axis location')
        coords = _point_coords(loc)
        origin = _vec(coords)
        axis = None
        refdir = None
        if len(entity.args) > 1 and isinstance(entity.args[1], IfcStepRef):
            axis = _vec(
                _point_coords(index.require(entity.args[1], 'axis'))
            )
        if len(entity.args) > 2 and isinstance(entity.args[2], IfcStepRef):
            refdir = _vec(
                _point_coords(
                    index.require(entity.args[2], 'ref direction')
                )
            )
        x, y, z = _orthonormal_frame(axis, refdir)
        return _mat_from_frame(x, y, z, origin)
    if entity.name == 'IFCAXIS2PLACEMENT2D':
        loc = index.require(entity.args[0], 'axis2d location')
        coords = _point_coords(loc)
        if len(coords) != 2:
            raise IfcInteropError(
                f'#{entity.entity_id}: IfcAxis2Placement2D needs 2D '
                'location'
            )
        angle = 0.0
        if len(entity.args) > 1 and isinstance(entity.args[1], IfcStepRef):
            d = _point_coords(index.require(entity.args[1], 'ref dir'))
            if len(d) >= 2:
                angle = atan2(d[1], d[0])
        ca = _cos(angle)
        sa = _sin(angle)
        return _mat_from_frame(
            (ca, sa, 0.0),
            (-sa, ca, 0.0),
            (0.0, 0.0, 1.0),
            (coords[0], coords[1], 0.0),
        )
    raise IfcInteropError(
        f'#{entity.entity_id}: unsupported placement {entity.name}'
    )


def _cos(angle: float) -> float:
    from math import cos

    return cos(angle)


def _sin(angle: float) -> float:
    from math import sin

    return sin(angle)


def _local_placement(
    index: _Index,
    entity: IfcStepEntity,
    chain: list[int],
) -> Mat4:
    """IfcLocalPlacement -> world (source-unit) 4x4.

    PlacementRelTo chains compose: world = parent_world * relative.
    """
    if entity.entity_id in chain:
        raise IfcInteropError('cyclic IfcLocalPlacement chain')
    chain.append(entity.entity_id)
    relative = _axis_placement(index, entity.args[1])
    parent = entity.args[0] if entity.args else IfcStepOmitted()
    if isinstance(parent, IfcStepRef):
        parent_entity = index.require(parent, 'placement parent')
        if parent_entity.name == 'IFCLOCALPLACEMENT':
            parent_mat = _local_placement(index, parent_entity, chain)
            return _mat_mul(parent_mat, relative)
        raise IfcInteropError(
            f'#{entity.entity_id}: unsupported placement parent '
            f'{parent_entity.name}'
        )
    return relative


_IDENTITY: Mat4 = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)


def _object_placement(
    index: _Index, entity: IfcStepEntity
) -> tuple[Mat4, tuple[int, ...]]:
    placement_arg = entity.args[5] if len(entity.args) > 5 else None
    if isinstance(placement_arg, IfcStepRef):
        placement = index.require(placement_arg, 'object placement')
        if placement.name == 'IFCLOCALPLACEMENT':
            chain: list[int] = []
            mat = _local_placement(index, placement, chain)
            return mat, tuple(chain)
        raise IfcInteropError(
            f'#{entity.entity_id}: unsupported placement '
            f'{placement.name}'
        )
    return _IDENTITY, ()


def _si_unit_scale(
    index: _Index, entity: IfcStepEntity
) -> tuple[str, float] | None:
    """IfcSIUnit -> (unit name incl. prefix, scale to SI base)."""
    if entity.name != 'IFCSIUNIT' or len(entity.args) < 4:
        return None
    name = _arg_enum_name(entity.args[3])
    prefix = _arg_enum_name(entity.args[2])
    if name is None:
        return None
    base = _SI_UNIT_BASE.get(name)
    if base is None:
        return None
    label, factor = base
    scale = factor
    full = label
    if prefix is not None:
        prefix_scale = _SI_PREFIX_SCALE.get(prefix)
        if prefix_scale is None:
            return None
        scale *= prefix_scale
        full = f'{prefix.lower()}{label}'
    return full, scale


def _conversion_unit_scale(
    index: _Index, entity: IfcStepEntity
) -> tuple[str, float] | None:
    """IfcConversionBasedUnit -> (declared name, scale via measure)."""
    if entity.name != 'IFCCONVERSIONBASEDUNIT' or len(entity.args) < 4:
        return None
    name = _arg_str(entity.args[2])
    factor_ref = entity.args[3]
    factor = index.get(factor_ref)
    if factor is None or factor.name != 'IFCMEASUREWITHUNIT':
        return (name or 'conversion_unit', 0.0)
    if len(factor.args) < 2:
        return None
    value_component = factor.args[0]
    unit_entity = index.get(factor.args[1])
    ratio: float | None = None
    if isinstance(value_component, IfcStepTypedValue) and (
        value_component.args
    ):
        inner = value_component.args[0]
        if isinstance(inner, (int, float)):
            ratio = float(inner)
    elif isinstance(value_component, (int, float)):
        ratio = float(value_component)
    if unit_entity is not None and unit_entity.name == 'IFCSIUNIT':
        si = _si_unit_scale(index, unit_entity)
        if si is None or ratio is None:
            return (name or 'conversion_unit', 0.0)
        label, scale = si
        return (name or label, ratio * scale)
    return (name or 'conversion_unit', 0.0)


def _resolve_units(
    index: _Index, project: IfcStepEntity
) -> tuple[IfcCoordinateAuthority, list[str]]:
    warnings: list[str] = []
    length_name: str | None = None
    length_scale: float | None = None
    length_state: IfcUnitState = 'undeclared'
    angle_name: str | None = None
    angle_scale: float | None = None

    # IfcProject attribute order: ... RepresentationContexts(7),
    # UnitsInContext(8).
    units_ref = project.args[8] if len(project.args) > 8 else None
    assignment = index.get(units_ref)
    length_seen: list[tuple[str, float]] = []
    if assignment is not None and assignment.name == 'IFCUNITASSIGNMENT':
        raw_units = assignment.args[0] if assignment.args else ()
        if not isinstance(raw_units, tuple):
            raw_units = (raw_units,)
        for unit_ref in raw_units:
            unit = index.get(unit_ref)
            if unit is None:
                continue
            unit_type = (
                _arg_enum_name(unit.args[1]) if len(unit.args) > 1 else None
            )
            resolved: tuple[str, float] | None = None
            if unit.name == 'IFCSIUNIT':
                resolved = _si_unit_scale(index, unit)
            elif unit.name == 'IFCCONVERSIONBASEDUNIT':
                resolved = _conversion_unit_scale(index, unit)
            if unit_type == 'LENGTHUNIT' and resolved is not None:
                length_seen.append(resolved)
            elif unit_type == 'PLANEANGLEUNIT' and resolved is not None:
                angle_name, angle_scale = resolved

    if len(length_seen) == 1:
        length_name, length_scale = length_seen[0]
        length_state = 'declared'
    elif len(length_seen) > 1:
        length_state = 'ambiguous'
        warnings.append('multiple length units declared; scale withheld')

    return (
        IfcCoordinateAuthority(
            length_unit_name=length_name,
            length_unit_state=length_state,
            length_scale_to_meter=length_scale,
            plane_angle_unit_name=angle_name,
            plane_angle_scale_to_radian=angle_scale,
            axis_convention=IFC_AXIS_CONVENTION,
        ),
        warnings,
    )


def _resolve_context_georef(
    index: _Index,
    project: IfcStepEntity,
    coordinate: IfcCoordinateAuthority,
    warnings: list[str],
) -> IfcCoordinateAuthority:
    """IfcGeometricRepresentationContext: WCS + TrueNorth; IfcMapConversion."""
    updates: dict[str, Any] = {}
    contexts: list[IfcStepEntity] = []
    # RepresentationContexts is attribute index 7 of IfcProject.
    if len(project.args) > 7 and isinstance(project.args[7], tuple):
        for ref in project.args[7]:
            ctx = index.get(ref)
            if ctx is not None:
                contexts.append(ctx)
    for ctx in contexts:
        if ctx.name not in (
            'IFCGEOMETRICREPRESENTATIONCONTEXT',
            'IFCGEOMETRICREPRESENTATIONSUBCONTEXT',
        ):
            continue
        dims = _arg_num(ctx.args[2]) if len(ctx.args) > 2 else None
        if dims != 3.0:
            continue
        # IfcGeometricRepresentationContext args: ContextIdentifier(0),
        # ContextType(1), CoordinateSpaceDimension(2), Precision(3),
        # WorldCoordinateSystem(4), TrueNorth(5).
        if len(ctx.args) > 4 and isinstance(ctx.args[4], IfcStepRef):
            wcs = index.get(ctx.args[4])
            if wcs is not None and wcs.name in (
                'IFCAXIS2PLACEMENT3D',
                'IFCAXIS2PLACEMENT2D',
            ):
                mat = _axis_placement(index, ctx.args[4])
                updates['wcs_origin_source_units'] = (
                    mat[0][3],
                    mat[1][3],
                    mat[2][3],
                )
        if len(ctx.args) > 5 and isinstance(ctx.args[5], IfcStepRef):
            tn = index.get(ctx.args[5])
            if tn is not None and tn.name == 'IFCDIRECTION':
                coords = _point_coords(tn)
                if len(coords) >= 2:
                    mag = (coords[0] ** 2 + coords[1] ** 2) ** 0.5
                    if mag > 0.0:
                        updates['true_north_vector'] = (
                            coords[0] / mag,
                            coords[1] / mag,
                        )
                        updates['true_north_state'] = 'declared'

    # IfcMapConversion — georeferencing declaration (bSI User Guide for
    # Geo-referencing in IFC): Eastings/Northings/OrthogonalHeight map the
    # local engineering CRS into the declared projected CRS. Recorded as
    # a declaration; HTDT coordinates stay in the local frame.
    map_conversions = index.typed('IFCMAPCONVERSION')
    if map_conversions:
        conv = map_conversions[0]
        if len(map_conversions) > 1:
            warnings.append(
                'multiple IfcMapConversion entries; first recorded'
            )
        if len(conv.args) >= 5:
            updates['georef_state'] = 'declared'
            target = index.get(conv.args[1])
            if target is not None and target.name == 'IFCPROJECTEDCRS':
                updates['georef_target_crs_name'] = _arg_str(target.args[0])
            source_ctx = index.get(conv.args[0])
            if source_ctx is not None:
                updates['georef_source_crs_context'] = (
                    _arg_str(source_ctx.args[1])
                    if len(source_ctx.args) > 1
                    else None
                )
            updates['georef_eastings'] = _arg_num(conv.args[2])
            updates['georef_northings'] = _arg_num(conv.args[3])
            updates['georef_orthogonal_height'] = _arg_num(conv.args[4])
            if len(conv.args) > 5:
                updates['georef_x_axis_easting'] = _arg_num(conv.args[5])
            if len(conv.args) > 6:
                updates['georef_x_axis_northing'] = _arg_num(conv.args[6])
            if len(conv.args) > 7:
                updates['georef_scale'] = _arg_num(conv.args[7])

    if coordinate.length_unit_state == 'undeclared':
        warnings.append(
            'no length unit declared on IfcProject; '
            'metre-normalized coordinates withheld'
        )
    return coordinate.model_copy(update=updates)


def _extract_properties(
    index: _Index,
    entity_id: int,
    rel_defines: Mapping[int, list[IfcStepEntity]],
    warnings: list[str],
) -> tuple[dict[str, IfcPropertyValue], int]:
    """IfcRelDefinesByProperties -> IfcPropertySet -> single values.

    Returns (properties, dropped_count) — the cap is counted honestly.
    """
    out: dict[str, IfcPropertyValue] = {}
    dropped = 0
    for rel in rel_defines.get(entity_id, ()):
        pset = index.get(rel.args[5] if len(rel.args) > 5 else None)
        if pset is None or pset.name != 'IFCPROPERTYSET':
            continue
        if len(pset.args) < 5 or not isinstance(pset.args[4], tuple):
            continue
        for prop_ref in pset.args[4]:
            prop = index.get(prop_ref)
            if prop is None or prop.name != 'IFCPROPERTYSINGLEVALUE':
                continue
            # IfcPropertySingleValue: Name(0), Description(1),
            # NominalValue(2), Unit(3).
            if len(prop.args) < 3:
                continue
            name = _arg_str(prop.args[0])
            if name is None:
                continue
            if len(out) >= _MAX_PROPERTIES_PER_ENTITY:
                dropped += 1
                continue
            nominal = prop.args[2]
            value: Any = None
            type_name = 'UNKNOWN'
            if isinstance(nominal, IfcStepTypedValue):
                type_name = nominal.type_name
                if nominal.args:
                    inner = nominal.args[0]
                    if isinstance(inner, IfcStepEnum):
                        value = {
                            'T': True,
                            'F': False,
                            'U': None,
                        }.get(inner.name, inner.name)
                    elif isinstance(inner, (int, float, str)):
                        value = inner
            elif isinstance(nominal, IfcStepEnum):
                type_name = 'ENUM'
                value = nominal.name
            elif isinstance(nominal, (int, float, str)):
                type_name = 'RAW'
                value = nominal
            unit_ref = _arg_ref(prop.args[3]) if len(prop.args) > 3 else None
            out[name] = IfcPropertyValue(
                name=name,
                ifc_value_type=type_name,
                value=value,
                unit_entity_id=unit_ref,
            )
    if dropped:
        warnings.append(
            f'entity #{entity_id}: property count capped; '
            f'{dropped} properties dropped'
        )
    return out, dropped


def _extract_materials(
    index: _Index,
    entity_id: int,
    rel_materials: Mapping[int, list[IfcStepEntity]],
    scale: float | None,
) -> tuple[IfcMaterialLayerEvidence, ...]:
    """IfcRelAssociatesMaterial -> layer set -> layer names/thicknesses."""
    layers: list[IfcMaterialLayerEvidence] = []
    for rel in rel_materials.get(entity_id, ()):
        mat_ref = rel.args[5] if len(rel.args) > 5 else None
        mat = index.get(mat_ref)
        if mat is None:
            continue
        layer_set: IfcStepEntity | None = None
        if mat.name == 'IFCMATERIALLAYERSETUSAGE':
            layer_set = index.get(mat.args[0]) if mat.args else None
        elif mat.name == 'IFCMATERIALLAYERSET':
            layer_set = mat
        elif mat.name == 'IFCMATERIAL':
            layers.append(
                IfcMaterialLayerEvidence(material_name=_arg_str(mat.args[0]))
            )
            continue
        elif mat.name == 'IFCMATERIALLIST':
            raw = mat.args[0] if mat.args else ()
            if isinstance(raw, tuple):
                for mref in raw:
                    m = index.get(mref)
                    if m is not None and m.name == 'IFCMATERIAL':
                        layers.append(
                            IfcMaterialLayerEvidence(
                                material_name=_arg_str(m.args[0])
                            )
                        )
            continue
        if layer_set is None or layer_set.name != 'IFCMATERIALLAYERSET':
            continue
        raw_layers = layer_set.args[0] if layer_set.args else ()
        if not isinstance(raw_layers, tuple):
            continue
        for lref in raw_layers:
            layer = index.get(lref)
            if layer is None or layer.name != 'IFCMATERIALLAYER':
                continue
            thickness = (
                _arg_num(layer.args[1]) if len(layer.args) > 1 else None
            )
            material_entity = index.get(layer.args[0]) if layer.args else None
            mat_name = (
                _arg_str(material_entity.args[0])
                if material_entity is not None
                and material_entity.name == 'IFCMATERIAL'
                else None
            )
            ventilated = None
            if len(layer.args) > 2 and isinstance(layer.args[2], IfcStepEnum):
                ventilated = layer.args[2].name == 'T'
            layers.append(
                IfcMaterialLayerEvidence(
                    material_name=mat_name,
                    layer_thickness_source_units=thickness,
                    layer_thickness_m=(
                        thickness * scale
                        if (thickness is not None and scale is not None)
                        else None
                    ),
                    is_ventilated=ventilated,
                    name=(
                        _arg_str(layer.args[3])
                        if len(layer.args) > 3
                        else None
                    ),
                )
            )
    return tuple(layers)


def _extract_geometry(
    index: _Index,
    entity: IfcStepEntity,
    warnings: list[str],
) -> tuple[tuple[IfcGeometryDescriptor, ...], tuple[str, ...]]:
    """Bounded geometry extraction per element.

    Reads IfcShapeRepresentation items of kinds this authority supports;
    all others are reported per item type, never silently skipped.
    """
    descriptors: list[IfcGeometryDescriptor] = []
    unresolved: list[str] = []
    product_shape = entity.args[6] if len(entity.args) > 6 else None
    shape = index.get(product_shape)
    if shape is None or shape.name != 'IFCPRODUCTDEFINITIONSHAPE':
        return (), ()
    reps = shape.args[2] if len(shape.args) > 2 else ()
    if not isinstance(reps, tuple):
        return (), ()
    for rep_ref in reps:
        rep = index.get(rep_ref)
        if rep is None or rep.name != 'IFCSHAPEREPRESENTATION':
            continue
        identifier = _arg_str(rep.args[1]) if len(rep.args) > 1 else None
        rep_type = _arg_str(rep.args[2]) if len(rep.args) > 2 else None
        items = rep.args[3] if len(rep.args) > 3 else ()
        if not isinstance(items, tuple):
            items = ()
        for item_ref in items:
            item = index.get(item_ref)
            if item is None:
                continue
            desc = _geometry_item(index, item, identifier, rep_type, warnings)
            if desc is not None:
                descriptors.append(desc)
            else:
                unresolved.append(item.name)
    return tuple(descriptors), tuple(unresolved)


def _geometry_item(
    index: _Index,
    item: IfcStepEntity,
    identifier: str | None,
    rep_type: str | None,
    warnings: list[str],
) -> IfcGeometryDescriptor | None:
    if item.name == 'IFCEXTRUDEDAREASOLID':
        try:
            profile = index.require(item.args[0], 'swept area')
            points: tuple[tuple[float, float], ...] = ()
            profile_kind = 'arbitrary'
            if profile.name == 'IFCRECTANGLEPROFILEDEF':
                # IfcRectangleProfileDef: ProfileType, ProfileName,
                # Position, XDim(3), YDim(4).
                x = (
                    _arg_num(profile.args[3])
                    if len(profile.args) > 3
                    else None
                )
                y = (
                    _arg_num(profile.args[4])
                    if len(profile.args) > 4
                    else None
                )
                if x is not None and y is not None:
                    hx, hy = x / 2.0, y / 2.0
                    points = ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy))
                    profile_kind = 'rectangle'
            elif profile.name == 'IFCARBITRARYCLOSEDPROFILEDEF':
                # ProfileType(0), ProfileName(1), OuterCurve(2).
                outer = (
                    index.get(profile.args[2])
                    if len(profile.args) > 2
                    else None
                )
                if outer is not None and outer.name == 'IFCPOLYLINE':
                    pts = []
                    for pref in (outer.args[0] if outer.args else ()):
                        p = index.get(pref)
                        if p is not None and p.name == 'IFCCARTESIANPOINT':
                            c = _point_coords(p)
                            if len(c) >= 2:
                                pts.append((c[0], c[1]))
                    points = tuple(pts)
            direction = None
            if len(item.args) > 2 and isinstance(item.args[2], IfcStepRef):
                d = index.get(item.args[2])
                if d is not None and d.name == 'IFCDIRECTION':
                    direction = _vec(_point_coords(d))
            depth = _arg_num(item.args[3]) if len(item.args) > 3 else None
            # IfcProfileDef: ProfileType(0), ProfileName(1).
            profile_name = (
                _arg_str(profile.args[1])
                if len(profile.args) > 1
                else None
            )
            payload = {
                'kind': 'extruded_area_solid',
                'profile_kind': profile_kind,
                'profile_name': profile_name,
                'points': points,
                'direction': direction,
                'depth': depth,
                'identifier': identifier,
                'representation_type': rep_type,
            }
            return IfcGeometryDescriptor(
                kind='extruded_area_solid',
                identifier=identifier,
                representation_type=rep_type,
                profile_name=profile_name,
                profile_points=points,
                extrusion_direction=direction,
                depth_source_units=depth,
                fingerprint=_digest(payload),
            )
        except IfcInteropError:
            warnings.append(
                f'#{item.entity_id}: extruded area solid could not be read'
            )
            return None
    if item.name == 'IFCFACETEDBREP':
        try:
            shell = index.require(item.args[0], 'closed shell')
            faces = shell.args[0] if shell.args else ()
            vertices: list[Vec3] = []
            if isinstance(faces, tuple):
                for fref in faces:
                    face = index.get(fref)
                    if face is None:
                        continue
                    for bref in (face.args[0] if face.args else ()):
                        bound = index.get(bref)
                        if bound is None:
                            continue
                        loop = index.get(bound.args[0]) if bound.args else None
                        if loop is None:
                            continue
                        for pref in (loop.args[0] if loop.args else ()):
                            p = index.get(pref)
                            if (
                                p is not None
                                and p.name == 'IFCCARTESIANPOINT'
                            ):
                                c = _point_coords(p)
                                if len(c) == 3:
                                    vertices.append(_vec(c))
            bmin = None
            bmax = None
            if vertices:
                bmin = (
                    min(v[0] for v in vertices),
                    min(v[1] for v in vertices),
                    min(v[2] for v in vertices),
                )
                bmax = (
                    max(v[0] for v in vertices),
                    max(v[1] for v in vertices),
                    max(v[2] for v in vertices),
                )
            payload = {
                'kind': 'faceted_brep',
                'vertices': vertices,
                'identifier': identifier,
            }
            return IfcGeometryDescriptor(
                kind='faceted_brep',
                identifier=identifier,
                representation_type=rep_type,
                vertices=tuple(vertices),
                bounds_min=bmin,
                bounds_max=bmax,
                fingerprint=_digest(payload),
            )
        except IfcInteropError:
            warnings.append(
                f'#{item.entity_id}: faceted brep could not be read'
            )
            return None
    if item.name == 'IFCBOUNDINGBOX':
        try:
            corner = index.require(item.args[0], 'bounding box corner')
            c = _point_coords(corner)
            xd = _arg_num(item.args[1]) if len(item.args) > 1 else 0.0
            yd = _arg_num(item.args[2]) if len(item.args) > 2 else 0.0
            zd = _arg_num(item.args[3]) if len(item.args) > 3 else 0.0
            payload = {
                'kind': 'bounding_box',
                'corner': c,
                'dims': (xd, yd, zd),
            }
            return IfcGeometryDescriptor(
                kind='bounding_box',
                identifier=identifier,
                representation_type=rep_type,
                bounds_min=_vec(c),
                bounds_max=(
                    c[0] + (xd or 0.0),
                    c[1] + (yd or 0.0),
                    c[2] + (zd or 0.0),
                ),
                fingerprint=_digest(payload),
            )
        except IfcInteropError:
            return None
    return None


#: Entity types the resolver understands structurally — everything else
#: lands in coverage.unhandled_entity_types (honest accounting, §1).
_COVERED_TYPES = set(IFC_SEMANTIC_ROLES) | {
    'IFCPROJECT',
    'IFCSITE',
    'IFCBUILDING',
    'IFCBUILDINGSTOREY',
    'IFCLOCALPLACEMENT',
    'IFCAXIS2PLACEMENT3D',
    'IFCAXIS2PLACEMENT2D',
    'IFCCARTESIANPOINT',
    'IFCDIRECTION',
    'IFCSIUNIT',
    'IFCCONVERSIONBASEDUNIT',
    'IFCMEASUREWITHUNIT',
    'IFCUNITASSIGNMENT',
    'IFCDIMENSIONALEXPONENTS',
    'IFCGEOMETRICREPRESENTATIONCONTEXT',
    'IFCGEOMETRICREPRESENTATIONSUBCONTEXT',
    'IFCMAPCONVERSION',
    'IFCPROJECTEDCRS',
    'IFCPRODUCTDEFINITIONSHAPE',
    'IFCSHAPEREPRESENTATION',
    'IFCEXTRUDEDAREASOLID',
    'IFCRECTANGLEPROFILEDEF',
    'IFCARBITRARYCLOSEDPROFILEDEF',
    'IFCPOLYLINE',
    'IFCFACETEDBREP',
    'IFCCLOSEDSHELL',
    'IFCFACE',
    'IFCFACEOUTERBOUND',
    'IFCIFCFACEBOUND',
    'IFCPOLYLOOP',
    'IFCBOUNDINGBOX',
    'IFCPROPERTYSET',
    'IFCPROPERTYSINGLEVALUE',
    'IFCMATERIAL',
    'IFCMATERIALLAYER',
    'IFCMATERIALLAYERSET',
    'IFCMATERIALLAYERSETUSAGE',
    'IFCMATERIALLIST',
    'IFCOWNERHISTORY',
    'IFCPERSON',
    'IFCORGANIZATION',
    'IFCPERSONANDORGANIZATION',
    'IFCAPPLICATION',
}


def resolve_ifc_model(
    header: IfcStepHeader,
    entities: Sequence[IfcStepEntity],
) -> IfcResolvedModel:
    """Resolve a parsed STEP document into the semantic model.

    Raises :class:`IfcInteropError` when no usable IfcProject exists —
    an IFC file without a project is structurally invalid for import.
    Every other gap is recorded in coverage instead.
    """
    index = _Index(entities)
    warnings: list[str] = []
    unhandled: dict[str, int] = {}
    unresolved_rep_items: dict[str, int] = {}
    relation_counts: dict[str, int] = {}
    dropped_props = 0
    unresolved_placements = 0

    schema_identifier = (
        header.schema_identifiers[0] if header.schema_identifiers else ''
    )
    schema_name = schema_identifier.split(' ')[0].upper()
    schema_supported = schema_name.startswith('IFC4')
    if not schema_supported:
        warnings.append(
            f'schema identifier {schema_identifier!r} is not a supported '
            'IFC4-family schema; entity resolution skipped'
        )

    projects = index.typed('IFCPROJECT')
    if not projects:
        raise IfcInteropError('no IfcProject in document')
    project = projects[0]
    if len(projects) > 1:
        warnings.append('multiple IfcProject entities; first used')

    coordinate, unit_warnings = _resolve_units(index, project)
    warnings.extend(unit_warnings)
    coordinate = _resolve_context_georef(
        index, project, coordinate, warnings
    )
    scale = coordinate.length_scale_to_meter
    wcs_origin = coordinate.wcs_origin_source_units or (0.0, 0.0, 0.0)
    coordinate = coordinate.model_copy(
        update={'origin_shift_applied': wcs_origin}
    )

    # Relationship indexes ------------------------------------------------
    rel_aggregates: dict[int, list[int]] = {}
    rel_aggregates_reverse: dict[int, list[int]] = {}
    containment: dict[int, list[int]] = {}
    voids: dict[int, list[int]] = {}
    fills: dict[int, int] = {}
    rel_defines: dict[int, list[IfcStepEntity]] = {}
    rel_materials: dict[int, list[IfcStepEntity]] = {}
    other_relation_counts: dict[str, int] = {}

    for entity in entities:
        if entity.name.startswith('IFCREL'):
            if entity.name in (
                'IFCRELAGGREGATES',
                'IFCRELCONTAINEDINSPATIALSTRUCTURE',
                'IFCRELVOIDSELEMENT',
                'IFCRELFILLSELEMENT',
                'IFCRELDEFINESBYPROPERTIES',
                'IFCRELASSOCIATESMATERIAL',
            ):
                continue
            other_relation_counts[entity.name] = (
                other_relation_counts.get(entity.name, 0) + 1
            )

    for rel in index.typed('IFCRELAGGREGATES'):
        relation_counts['IFCRELAGGREGATES'] = (
            relation_counts.get('IFCRELAGGREGATES', 0) + 1
        )
        # IfcRelDecomposes: RelatingObject(4), RelatedObjects(5).
        relating = _arg_ref(rel.args[4]) if len(rel.args) > 4 else None
        related = rel.args[5] if len(rel.args) > 5 else ()
        if relating is None or not isinstance(related, tuple):
            continue
        for r in related:
            rid = _arg_ref(r)
            if rid is None:
                continue
            rel_aggregates.setdefault(relating, []).append(rid)
            rel_aggregates_reverse.setdefault(rid, []).append(relating)
    for rel in index.typed('IFCRELCONTAINEDINSPATIALSTRUCTURE'):
        relation_counts['IFCRELCONTAINEDINSPATIALSTRUCTURE'] = (
            relation_counts.get('IFCRELCONTAINEDINSPATIALSTRUCTURE', 0) + 1
        )
        # RelatedElements(4), RelatingStructure(5).
        related = rel.args[4] if len(rel.args) > 4 else ()
        relating = _arg_ref(rel.args[5]) if len(rel.args) > 5 else None
        if relating is None or not isinstance(related, tuple):
            continue
        for r in related:
            rid = _arg_ref(r)
            if rid is not None:
                containment.setdefault(rid, []).append(relating)
    for rel in index.typed('IFCRELVOIDSELEMENT'):
        relation_counts['IFCRELVOIDSELEMENT'] = (
            relation_counts.get('IFCRELVOIDSELEMENT', 0) + 1
        )
        relating = _arg_ref(rel.args[4]) if len(rel.args) > 4 else None
        opening = _arg_ref(rel.args[5]) if len(rel.args) > 5 else None
        if relating is not None and opening is not None:
            voids.setdefault(relating, []).append(opening)
    for rel in index.typed('IFCRELFILLSELEMENT'):
        relation_counts['IFCRELFILLSELEMENT'] = (
            relation_counts.get('IFCRELFILLSELEMENT', 0) + 1
        )
        opening = _arg_ref(rel.args[4]) if len(rel.args) > 4 else None
        filling = _arg_ref(rel.args[5]) if len(rel.args) > 5 else None
        if opening is not None and filling is not None:
            fills[opening] = filling
    for rel in index.typed('IFCRELDEFINESBYPROPERTIES'):
        relation_counts['IFCRELDEFINESBYPROPERTIES'] = (
            relation_counts.get('IFCRELDEFINESBYPROPERTIES', 0) + 1
        )
        related = rel.args[4] if len(rel.args) > 4 else ()
        if not isinstance(related, tuple):
            continue
        for r in related:
            rid = _arg_ref(r)
            if rid is not None:
                rel_defines.setdefault(rid, []).append(rel)
    for rel in index.typed('IFCRELASSOCIATESMATERIAL'):
        relation_counts['IFCRELASSOCIATESMATERIAL'] = (
            relation_counts.get('IFCRELASSOCIATESMATERIAL', 0) + 1
        )
        related = rel.args[4] if len(rel.args) > 4 else ()
        if not isinstance(related, tuple):
            continue
        for r in related:
            rid = _arg_ref(r)
            if rid is not None:
                rel_materials.setdefault(rid, []).append(rel)
    relation_counts.update(other_relation_counts)

    spatial_kind = {
        'IFCSITE': 'site',
        'IFCBUILDING': 'building',
        'IFCBUILDINGSTOREY': 'storey',
        'IFCSPACE': 'space',
    }

    def _parent_of(entity_id: int) -> tuple[int | None, str | None]:
        for parent in containment.get(entity_id, ()):
            return parent, 'containment'
        for parent in rel_aggregates_reverse.get(entity_id, ()):
            return parent, 'aggregation'
        return None, None

    def _global_id(entity: IfcStepEntity | None) -> str | None:
        if entity is None or not entity.args:
            return None
        return _arg_str(entity.args[0])

    def _name_of(entity: IfcStepEntity | None) -> str | None:
        if entity is None or len(entity.args) < 3:
            return None
        return _arg_str(entity.args[2])

    # Spatial hierarchy ---------------------------------------------------
    spatial_nodes: list[IfcResolvedSpatialNode] = []
    spatial_names = {v: k for k, v in spatial_kind.items()}
    for entity in entities:
        kind = spatial_kind.get(entity.name)
        if kind is None:
            continue
        parent_id, _ = _parent_of(entity.entity_id)
        parent_entity = index.by_id.get(parent_id) if parent_id else None
        elev = (
            _arg_num(entity.args[9])
            if kind == 'storey' and len(entity.args) > 9
            else None
        )
        try:
            mat, _chain = _object_placement(index, entity)
        except IfcInteropError as exc:
            warnings.append(
                f'#{entity.entity_id} {entity.name}: placement '
                f'unresolved ({exc}); transform withheld'
            )
            mat = None
            unresolved_placements += 1
        spatial_nodes.append(
            IfcResolvedSpatialNode(
                step_entity_id=entity.entity_id,
                kind=kind,  # type: ignore[arg-type]
                global_id=_global_id(entity),
                name=_name_of(entity),
                parent_global_id=_global_id(parent_entity),
                elevation_source_units=elev,
                world_transform_source_units=(
                    _mat_flat(mat) if mat is not None else None
                ),
            )
        )
    _ = spatial_names

    # Coverage: unhandled entity types -----------------------------------
    for entity in entities:
        if entity.name.startswith('IFCREL'):
            continue
        if entity.name in _COVERED_TYPES:
            continue
        if entity.name in IFC_SEMANTIC_ROLES:
            continue
        unhandled[entity.name] = unhandled.get(entity.name, 0) + 1

    # Semantic elements ---------------------------------------------------
    elements: list[IfcResolvedElement] = []
    mapped_count = 0
    if schema_supported:
        for entity in entities:
            role = IFC_SEMANTIC_ROLES.get(entity.name)
            if role is None:
                continue
            mapped_count += 1
            try:
                mat, chain = _object_placement(index, entity)
            except IfcInteropError as exc:
                warnings.append(
                    f'#{entity.entity_id} {entity.name}: placement '
                    f'unresolved ({exc}); transform withheld'
                )
                mat, chain = None, ()
                unresolved_placements += 1
            mat_m = (
                _mat_scale_translation(mat, scale)
                if mat is not None and scale is not None
                else None
            )
            parent_id, parent_rel = _parent_of(entity.entity_id)
            parent_entity = (
                index.by_id.get(parent_id) if parent_id else None
            )

            props, dropped = _extract_properties(
                index, entity.entity_id, rel_defines, warnings
            )
            dropped_props += dropped
            materials = _extract_materials(
                index, entity.entity_id, rel_materials, scale
            )
            geom, unresolved = _extract_geometry(index, entity, warnings)
            for name in unresolved:
                unresolved_rep_items[name] = (
                    unresolved_rep_items.get(name, 0) + 1
                )

            openings: list[IfcOpeningLink] = []
            for opening_id in voids.get(entity.entity_id, ()):
                opening_ent = index.by_id.get(opening_id)
                filling_ent = index.by_id.get(fills.get(opening_id))
                openings.append(
                    IfcOpeningLink(
                        opening_global_id=_global_id(opening_ent),
                        opening_name=_name_of(opening_ent),
                        filling_global_id=_global_id(filling_ent),
                        filling_type=(
                            filling_ent.name
                            if filling_ent is not None
                            else None
                        ),
                    )
                )

            semantic_payload = {
                'ifc_type': entity.name,
                'global_id': _global_id(entity),
                'name': _name_of(entity),
                'role': role,
                'properties': {
                    k: (v.value, v.ifc_value_type)
                    for k, v in sorted(props.items())
                },
                'materials': [
                    (l.material_name, l.layer_thickness_source_units)
                    for l in materials
                ],
                'openings': [
                    (o.opening_global_id, o.filling_global_id, o.filling_type)
                    for o in openings
                ],
                'parent_global_id': _global_id(parent_entity),
            }
            geometry_payload = {
                'world_source': _mat_flat(mat) if mat is not None else None,
                'descriptors': [g.fingerprint for g in geom],
            }
            elements.append(
                IfcResolvedElement(
                    step_entity_id=entity.entity_id,
                    ifc_type=entity.name,
                    global_id=_global_id(entity),
                    name=_name_of(entity),
                    description=(
                        _arg_str(entity.args[3])
                        if len(entity.args) > 3
                        else None
                    ),
                    htdt_role=role,  # type: ignore[arg-type]
                    parent_global_id=_global_id(parent_entity),
                    parent_kind=parent_rel,
                    world_transform_source_units=(
                        _mat_flat(mat) if mat is not None else None
                    ),
                    world_transform_m=(
                        _mat_flat(mat_m) if mat_m is not None else None
                    ),
                    placement_chain_ids=chain,
                    properties=props,
                    material_layers=materials,
                    openings=tuple(openings),
                    geometry_descriptors=geom,
                    unresolved_representation_items=unresolved,
                    semantic_fingerprint=_digest(semantic_payload),
                    geometry_fingerprint=_digest(geometry_payload),
                )
            )

    coverage = IfcImportCoverage(
        entity_total=len(entities),
        mapped_entity_count=mapped_count,
        spatial_entity_count=len(spatial_nodes),
        unresolved_placement_count=unresolved_placements,
        unhandled_entity_types=unhandled,
        unresolved_representation_items=unresolved_rep_items,
        dropped_property_count=dropped_props,
        relation_counts=relation_counts,
        warnings=tuple(warnings),
    )
    return IfcResolvedModel(
        schema_identifier=schema_identifier,
        schema_supported=schema_supported,
        source_application=_arg_str(
            header.file_name_fields.get('originating_system')
        ),
        preprocessor_version=_arg_str(
            header.file_name_fields.get('preprocessor_version')
        ),
        source_timestamp=_arg_str(header.file_name_fields.get('time_stamp')),
        source_file_name=_arg_str(header.file_name_fields.get('name')),
        coordinate=coordinate,
        project_global_id=_global_id(project),
        project_name=_name_of(project),
        spatial_nodes=tuple(spatial_nodes),
        elements=tuple(elements),
        coverage=coverage,
    )


# ---------------------------------------------------------------------------
# Sealed authority records
# ---------------------------------------------------------------------------


class IfcImportArtifact(BaseModel):
    """Sealed provenance record for one imported IFC file (#578 §1)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    artifact_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    file_name: str = Field(min_length=1)
    file_sha256: str = Field(pattern=_SHA256_PATTERN)
    file_size_bytes: int = Field(ge=0)
    schema_identifier: str = ''
    schema_supported: bool = True
    importer_version: str = Field(min_length=1)
    source_application: str | None = None
    preprocessor_version: str | None = None
    source_timestamp: str | None = None
    project_global_id: str | None = None
    project_name: str | None = None
    coordinate: IfcCoordinateAuthority
    spatial_hierarchy: dict[str, tuple[str, ...]] = Field(
        default_factory=dict
    )
    intake_state: IfcIntakeOverall | None = None
    intake_evaluation_id: str | None = None
    coverage: IfcImportCoverage
    warnings: tuple[str, ...] = ()
    imported_at_utc: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'IfcImportArtifact':
        expected = _digest(self.identity_payload())
        if self.artifact_sha256 != expected:
            raise ValueError('artifact_sha256 does not match content')
        if self.artifact_id != f'iia:{expected}':
            raise ValueError('artifact_id must be iia:<sha256>')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('artifact_id', None)
        payload.pop('artifact_sha256', None)
        return payload


class IfcEntityMapping(BaseModel):
    """Stable per-entity mapping (#578 §3, §7)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    mapping_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    import_artifact_id: str = Field(min_length=1)
    import_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    step_entity_id: int = Field(ge=1)
    ifc_global_id: str | None = None
    ifc_type: str = Field(min_length=1)
    name: str | None = None
    htdt_role: IfcHtdtRole
    parent_global_id: str | None = None
    parent_kind: str | None = None
    world_transform_source_units: tuple[float, ...] | None = None
    """Resolved placement in source units; ``None`` when the placement
    chain could not be resolved — withheld, never faked (§2)."""
    world_transform_m: tuple[float, ...] | None = None
    placement_chain_ids: tuple[int, ...] = ()
    properties: dict[str, IfcPropertyValue] = Field(default_factory=dict)
    material_layers: tuple[IfcMaterialLayerEvidence, ...] = ()
    openings: tuple[IfcOpeningLink, ...] = ()
    geometry_descriptors: tuple[IfcGeometryDescriptor, ...] = ()
    unresolved_representation_items: tuple[str, ...] = ()
    semantic_fingerprint: str = Field(min_length=1)
    geometry_fingerprint: str = Field(min_length=1)
    reconciliation_state: IfcReconciliationState = 'new'
    mapping_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'IfcEntityMapping':
        expected = _digest(self.identity_payload())
        if self.mapping_sha256 != expected:
            raise ValueError('mapping_sha256 does not match content')
        if self.mapping_id != f'iem:{expected}':
            raise ValueError('mapping_id must be iem:<sha256>')
        _require_finite_tuple(
            self.world_transform_source_units,
            'world_transform_source_units',
        )
        if self.world_transform_source_units is not None and (
            len(self.world_transform_source_units) != 16
        ):
            raise ValueError('world transform must be a 4x4 matrix')
        _require_finite_tuple(self.world_transform_m, 'world_transform_m')
        if self.world_transform_m is not None and (
            len(self.world_transform_m) != 16
        ):
            raise ValueError('normalized transform must be a 4x4 matrix')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('mapping_id', None)
        payload.pop('mapping_sha256', None)
        return payload


class IfcDeltaItem(BaseModel):
    """One changed entity inside a revision delta."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    ifc_global_id: str | None
    prior_mapping_id: str | None = None
    new_mapping_id: str | None = None
    change_kind: IfcDeltaChangeKind
    acoustically_relevant: bool
    detail: str | None = None


class IfcRevisionDelta(BaseModel):
    """Sealed revision comparison between two imports (#578 §6)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    delta_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    prior_artifact_id: str = Field(min_length=1)
    prior_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    new_artifact_id: str = Field(min_length=1)
    new_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    items: tuple[IfcDeltaItem, ...] = ()
    unchanged_count: int = Field(ge=0)
    geometry_changed_count: int = Field(ge=0)
    semantics_changed_count: int = Field(ge=0)
    added_count: int = Field(ge=0)
    removed_count: int = Field(ge=0)
    ambiguous_count: int = Field(ge=0)
    acoustic_affected_count: int = Field(ge=0)
    reconciliation_state: Literal[
        'clean', 'needs_review', 'stale_marked'
    ] = 'clean'
    evaluated_at_utc: str = Field(min_length=1)
    delta_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'IfcRevisionDelta':
        expected = _digest(self.identity_payload())
        if self.delta_sha256 != expected:
            raise ValueError('delta_sha256 does not match content')
        if self.delta_id != f'ird:{expected}':
            raise ValueError('delta_id must be ird:<sha256>')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('delta_id', None)
        payload.pop('delta_sha256', None)
        return payload


class IfcIntakeRequirement(BaseModel):
    """One machine-checkable intake requirement (IDS-equivalent, §8)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    requirement_id: str = Field(min_length=1)
    kind: Literal[
        'schema_allowed',
        'length_unit_declared',
        'min_spaces',
        'storey_context',
        'geometric_representation',
        'opening_identity',
        'material_layers',
        'adjacent_space_relations',
    ]
    params: dict[str, Any] = Field(default_factory=dict)
    required: bool = True
    description_ja: str = ''


class IfcIntakeProfile(BaseModel):
    """Versioned HTDT BIM intake profile (#578 §8)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    requirements: tuple[IfcIntakeRequirement, ...] = ()
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'IfcIntakeProfile':
        expected = _digest(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('profile_sha256 does not match content')
        if self.profile_id != f'iip:{expected}':
            raise ValueError('profile_id must be iip:<sha256>')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('profile_id', None)
        payload.pop('profile_sha256', None)
        return payload


class IfcIntakeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    requirement_id: str = Field(min_length=1)
    verdict: IfcIntakeVerdict
    diagnostics: tuple[str, ...] = ()


class IfcIntakeEvaluation(BaseModel):
    """Sealed result of evaluating an intake profile against a document."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    evaluation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_id: str | None = None
    artifact_sha256: str | None = None
    file_sha256: str = Field(pattern=_SHA256_PATTERN)
    results: tuple[IfcIntakeResult, ...] = ()
    overall_state: IfcIntakeOverall
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'IfcIntakeEvaluation':
        expected = _digest(self.identity_payload())
        if self.evaluation_sha256 != expected:
            raise ValueError('evaluation_sha256 does not match content')
        if self.evaluation_id != f'iie:{expected}':
            raise ValueError('evaluation_id must be iie:<sha256>')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('evaluation_id', None)
        payload.pop('evaluation_sha256', None)
        return payload


class IfcExportPackage(BaseModel):
    """Bounded create-new export artifact (#578 §10)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    export_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    mode: IfcExportMode
    label: str = Field(min_length=1)
    source_artifact_id: str | None = None
    source_artifact_sha256: str | None = None
    provenance_note: str = ''
    included_sections: tuple[str, ...] = ()
    unexported_items: tuple[str, ...] = ()
    step_text: str = Field(min_length=1)
    step_sha256: str = Field(pattern=_SHA256_PATTERN)
    created_at_utc: str = Field(min_length=1)
    export_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'IfcExportPackage':
        expected = _digest(self.identity_payload())
        if self.export_sha256 != expected:
            raise ValueError('export_sha256 does not match content')
        if self.export_id != f'ixp:{expected}':
            raise ValueError('export_id must be ixp:<sha256>')
        if self.step_sha256 != sha256(
            self.step_text.encode('utf-8')
        ).hexdigest():
            raise ValueError('step_sha256 does not match step_text')
        if (self.source_artifact_id is None) != (
            self.source_artifact_sha256 is None
        ):
            raise ValueError(
                'source_artifact_id and source_artifact_sha256 must be '
                'set together'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('export_id', None)
        payload.pop('export_sha256', None)
        return payload


# ---------------------------------------------------------------------------
# Import pipeline
# ---------------------------------------------------------------------------


def build_ifc_import(
    *,
    document_id: str,
    file_name: str,
    source: bytes | str,
    imported_at_utc: str,
    intake_evaluation: IfcIntakeEvaluation | None = None,
) -> tuple[IfcImportArtifact, tuple[IfcEntityMapping, ...]]:
    """Parse + resolve + seal one IFC file.

    ``source`` is the raw file bytes (preferred — the SHA-256 covers them
    verbatim) or text. An intake evaluation computed for the same file is
    bound into the artifact when supplied.
    """
    if isinstance(source, bytes):
        raw = source
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise IfcStepParseError(
                'STEP file is not valid UTF-8'
            ) from exc
    else:
        text = source
        raw = text.encode('utf-8')
    file_sha = sha256(raw).hexdigest()
    header, entities = parse_ifc_step(text)
    model = resolve_ifc_model(header, entities)

    hierarchy: dict[str, list[str]] = {}
    for node in model.spatial_nodes:
        gid = node.global_id or f'#{node.step_entity_id}'
        children = [
            (n.global_id or f'#{n.step_entity_id}')
            for n in model.spatial_nodes
            if n.parent_global_id == node.global_id
        ]
        hierarchy[gid] = sorted(children)

    warnings = list(model.coverage.warnings)
    if intake_evaluation is not None and (
        intake_evaluation.file_sha256 != file_sha
    ):
        raise IfcInteropError(
            'intake evaluation was computed for a different file'
        )
    artifact = _seal_import_artifact(
        document_id=document_id,
        file_name=file_name,
        file_sha256=file_sha,
        file_size_bytes=len(raw),
        model=model,
        spatial_hierarchy={k: tuple(v) for k, v in hierarchy.items()},
        intake_evaluation=intake_evaluation,
        warnings=tuple(warnings),
        imported_at_utc=imported_at_utc,
    )
    mappings = tuple(
        _seal_entity_mapping(
            document_id=document_id,
            artifact=artifact,
            element=element,
        )
        for element in model.elements
    )
    return artifact, mappings


def _seal_import_artifact(
    *,
    document_id: str,
    file_name: str,
    file_sha256: str,
    file_size_bytes: int,
    model: IfcResolvedModel,
    spatial_hierarchy: dict[str, tuple[str, ...]],
    intake_evaluation: IfcIntakeEvaluation | None,
    warnings: tuple[str, ...],
    imported_at_utc: str,
) -> IfcImportArtifact:
    probe = IfcImportArtifact.model_construct(
        **_canon(
            IfcImportArtifact,
            dict(
                artifact_id='',
                document_id=document_id,
                file_name=file_name,
                file_sha256=file_sha256,
                file_size_bytes=file_size_bytes,
                schema_identifier=model.schema_identifier,
                schema_supported=model.schema_supported,
                importer_version=IFC_INTEROP_AUTHORITY_VERSION,
                source_application=model.source_application,
                preprocessor_version=model.preprocessor_version,
                source_timestamp=model.source_timestamp,
                project_global_id=model.project_global_id,
                project_name=model.project_name,
                coordinate=model.coordinate,
                spatial_hierarchy=spatial_hierarchy,
                intake_state=(
                    intake_evaluation.overall_state
                    if intake_evaluation is not None
                    else None
                ),
                intake_evaluation_id=(
                    intake_evaluation.evaluation_id
                    if intake_evaluation is not None
                    else None
                ),
                coverage=model.coverage,
                warnings=warnings,
                imported_at_utc=imported_at_utc,
                artifact_sha256='',
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return IfcImportArtifact(
        **probe.model_dump(exclude={'artifact_id', 'artifact_sha256'}),
        artifact_id=f'iia:{sha}',
        artifact_sha256=sha,
    )


def _seal_entity_mapping(
    *,
    document_id: str,
    artifact: IfcImportArtifact,
    element: IfcResolvedElement,
) -> IfcEntityMapping:
    probe = IfcEntityMapping.model_construct(
        **_canon(
            IfcEntityMapping,
            dict(
                mapping_id='',
                document_id=document_id,
                import_artifact_id=artifact.artifact_id,
                import_artifact_sha256=artifact.artifact_sha256,
                step_entity_id=element.step_entity_id,
                ifc_global_id=element.global_id,
                ifc_type=element.ifc_type,
                name=element.name,
                htdt_role=element.htdt_role,
                parent_global_id=element.parent_global_id,
                parent_kind=element.parent_kind,
                world_transform_source_units=(
                    element.world_transform_source_units
                ),
                world_transform_m=element.world_transform_m,
                placement_chain_ids=element.placement_chain_ids,
                properties=element.properties,
                material_layers=element.material_layers,
                openings=element.openings,
                geometry_descriptors=element.geometry_descriptors,
                unresolved_representation_items=(
                    element.unresolved_representation_items
                ),
                semantic_fingerprint=element.semantic_fingerprint,
                geometry_fingerprint=element.geometry_fingerprint,
                reconciliation_state='new',
                mapping_sha256='',
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return IfcEntityMapping(
        **probe.model_dump(exclude={'mapping_id', 'mapping_sha256'}),
        mapping_id=f'iem:{sha}',
        mapping_sha256=sha,
    )


def mark_mapping_reconciliation(
    mapping: IfcEntityMapping,
    state: IfcReconciliationState,
) -> IfcEntityMapping:
    """Re-seal a mapping with an updated reconciliation state.

    A state change produces a *new* sealed record — the append-only
    repository keeps the transition history; the latest record per
    (import, entity) is the current state.
    """
    data = mapping.model_dump(mode='python')
    data['reconciliation_state'] = state
    data['mapping_id'] = ''
    data['mapping_sha256'] = ''
    probe = IfcEntityMapping.model_construct(
        **_canon(IfcEntityMapping, data)
    )
    sha = _digest(probe.identity_payload())
    return IfcEntityMapping(
        **probe.model_dump(exclude={'mapping_id', 'mapping_sha256'}),
        mapping_id=f'iem:{sha}',
        mapping_sha256=sha,
    )


# ---------------------------------------------------------------------------
# Revision delta (issue #578 §6)
# ---------------------------------------------------------------------------


def compute_ifc_revision_delta(
    *,
    document_id: str,
    prior_artifact: IfcImportArtifact,
    prior_mappings: Sequence[IfcEntityMapping],
    new_artifact: IfcImportArtifact,
    new_mappings: Sequence[IfcEntityMapping],
    evaluated_at_utc: str,
) -> IfcRevisionDelta:
    """Diff a new source revision against the prior import.

    Matching keys on GlobalId where present, else on (ifc_type, name,
    parent) — never on step entity id alone, which is file-local noise.
    Geometry or build-up/opening changes are acoustically relevant;
    semantic-only changes are recorded but not flagged.
    """
    def _key(m: IfcEntityMapping) -> tuple[str, ...]:
        if m.ifc_global_id:
            return ('gid', m.ifc_global_id)
        return (
            'fuzzy',
            m.ifc_type,
            m.name or '',
            m.parent_global_id or '',
        )

    prior = {_key(m): m for m in prior_mappings}
    new = {_key(m): m for m in new_mappings}

    items: list[IfcDeltaItem] = []
    unchanged = geo = sem = added = removed = ambiguous = 0
    acoustic = 0

    for key, nm in new.items():
        pm = prior.get(key)
        if pm is None:
            # Possible ambiguous rebind: same storey/type/name but the
            # GlobalId changed — the external tool recreated the object
            # (#578 §6: explicit reconciliation, no silent rebind).
            candidates = [
                p
                for p in prior_mappings
                if p.ifc_type == nm.ifc_type
                and p.name == nm.name
                and p.parent_global_id == nm.parent_global_id
                and p.ifc_global_id != nm.ifc_global_id
                and p.mapping_id
                not in {i.prior_mapping_id for i in items}
            ]
            if candidates:
                ambiguous += 1
                items.append(
                    IfcDeltaItem(
                        ifc_global_id=nm.ifc_global_id,
                        prior_mapping_id=candidates[0].mapping_id,
                        new_mapping_id=nm.mapping_id,
                        change_kind='ambiguous_rebind',
                        acoustically_relevant=True,
                        detail=(
                            'GlobalId changed but type/name/parent match a '
                            'prior mapping; requires explicit reconciliation'
                        ),
                    )
                )
                acoustic += 1
            else:
                added += 1
                items.append(
                    IfcDeltaItem(
                        ifc_global_id=nm.ifc_global_id,
                        new_mapping_id=nm.mapping_id,
                        change_kind='added',
                        acoustically_relevant=nm.htdt_role
                        in ('room_candidate', 'boundary', 'opening'),
                    )
                )
            continue
        if pm.semantic_fingerprint == nm.semantic_fingerprint and (
            pm.geometry_fingerprint == nm.geometry_fingerprint
        ):
            unchanged += 1
            items.append(
                IfcDeltaItem(
                    ifc_global_id=nm.ifc_global_id,
                    prior_mapping_id=pm.mapping_id,
                    new_mapping_id=nm.mapping_id,
                    change_kind='unchanged',
                    acoustically_relevant=False,
                )
            )
            continue
        if pm.geometry_fingerprint != nm.geometry_fingerprint:
            geo += 1
            acoustic += 1
            items.append(
                IfcDeltaItem(
                    ifc_global_id=nm.ifc_global_id,
                    prior_mapping_id=pm.mapping_id,
                    new_mapping_id=nm.mapping_id,
                    change_kind='geometry_changed',
                    acoustically_relevant=True,
                    detail='placement or geometry fingerprints differ',
                )
            )
        else:
            sem += 1
            # Build-up/opening changes are acoustic even when the world
            # transform stayed identical (#578 §6 delta list).
            buildup_changed = (
                pm.material_layers != nm.material_layers
                or pm.openings != nm.openings
            )
            if buildup_changed:
                acoustic += 1
            items.append(
                IfcDeltaItem(
                    ifc_global_id=nm.ifc_global_id,
                    prior_mapping_id=pm.mapping_id,
                    new_mapping_id=nm.mapping_id,
                    change_kind='semantics_changed',
                    acoustically_relevant=buildup_changed,
                    detail=(
                        'material layers or openings changed'
                        if buildup_changed
                        else 'non-geometric property change'
                    ),
                )
            )

    for key, pm in prior.items():
        if key not in new:
            if any(i.prior_mapping_id == pm.mapping_id for i in items):
                continue
            removed += 1
            items.append(
                IfcDeltaItem(
                    ifc_global_id=pm.ifc_global_id,
                    prior_mapping_id=pm.mapping_id,
                    change_kind='removed',
                    acoustically_relevant=pm.htdt_role
                    in ('room_candidate', 'boundary', 'opening'),
                    detail='entity missing in new revision — stale',
                )
            )
            if pm.htdt_role in ('room_candidate', 'boundary', 'opening'):
                acoustic += 1

    # Project-level declarations: a unit/north/georef change shifts every
    # coordinate — acoustically relevant by construction (#578 §2).
    coordinate_changed = (
        prior_artifact.coordinate != new_artifact.coordinate
    )
    if coordinate_changed:
        sem += 1
        acoustic += 1
        items.append(
            IfcDeltaItem(
                ifc_global_id=None,
                change_kind='semantics_changed',
                acoustically_relevant=True,
                detail=(
                    'coordinate authority changed (units/north/georef) — '
                    'all mapped transforms must be re-evaluated'
                ),
            )
        )

    if ambiguous or coordinate_changed:
        state = 'needs_review'
    elif removed or geo:
        state = 'stale_marked'
    else:
        state = 'clean'

    probe = IfcRevisionDelta.model_construct(
        **_canon(
            IfcRevisionDelta,
            dict(
                delta_id='',
                document_id=document_id,
                prior_artifact_id=prior_artifact.artifact_id,
                prior_artifact_sha256=prior_artifact.artifact_sha256,
                new_artifact_id=new_artifact.artifact_id,
                new_artifact_sha256=new_artifact.artifact_sha256,
                items=tuple(items),
                unchanged_count=unchanged,
                geometry_changed_count=geo,
                semantics_changed_count=sem,
                added_count=added,
                removed_count=removed,
                ambiguous_count=ambiguous,
                acoustic_affected_count=acoustic,
                reconciliation_state=state,
                evaluated_at_utc=evaluated_at_utc,
                delta_sha256='',
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return IfcRevisionDelta(
        **probe.model_dump(exclude={'delta_id', 'delta_sha256'}),
        delta_id=f'ird:{sha}',
        delta_sha256=sha,
    )


# ---------------------------------------------------------------------------
# Intake profile (IDS-equivalent, issue #578 §8)
# ---------------------------------------------------------------------------


def build_ifc_intake_profile(
    *,
    document_id: str,
    name: str,
    profile_version: str,
    requirements: Sequence[IfcIntakeRequirement],
) -> IfcIntakeProfile:
    probe = IfcIntakeProfile.model_construct(
        **_canon(
            IfcIntakeProfile,
            dict(
                profile_id='',
                document_id=document_id,
                name=name,
                profile_version=profile_version,
                requirements=tuple(requirements),
                profile_sha256='',
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return IfcIntakeProfile(
        **probe.model_dump(exclude={'profile_id', 'profile_sha256'}),
        profile_id=f'iip:{sha}',
        profile_sha256=sha,
    )


def _eval_requirement(
    req: IfcIntakeRequirement,
    header: IfcStepHeader,
    index: _Index,
    model: IfcResolvedModel | None,
) -> IfcIntakeResult:
    diagnostics: list[str] = []

    if req.kind == 'schema_allowed':
        allowed = tuple(
            str(v).upper()
            for v in req.params.get('allowed_identifiers', ())
        ) or SUPPORTED_IFC_SCHEMA_IDENTIFIERS
        found = (
            header.schema_identifiers[0].split(' ')[0].upper()
            if header.schema_identifiers
            else ''
        )
        ok = any(
            found == a or found.startswith(a.rstrip('*'))
            for a in allowed
        )
        if not ok:
            diagnostics.append(
                f'missing_schema: FILE_SCHEMA reports '
                f'{header.schema_identifiers!r}; allowed {allowed!r}'
            )
        return IfcIntakeResult(
            requirement_id=req.requirement_id,
            verdict='satisfied' if ok else 'unsatisfied',
            diagnostics=tuple(diagnostics),
        )

    if model is None:
        return IfcIntakeResult(
            requirement_id=req.requirement_id,
            verdict='unsatisfied',
            diagnostics=(
                'model_resolution_failed: schema unsupported or '
                'structurally invalid; model requirements unsatisfied',
            ),
        )

    if req.kind == 'length_unit_declared':
        if model.coordinate.length_unit_state == 'declared':
            return IfcIntakeResult(
                requirement_id=req.requirement_id,
                verdict='satisfied',
            )
        diagnostics.append(
            'missing_length_unit: no usable LENGTHUNIT under '
            'IfcProject.UnitsInContext (state=%s)'
            % model.coordinate.length_unit_state
        )
        return IfcIntakeResult(
            requirement_id=req.requirement_id,
            verdict='unsatisfied',
            diagnostics=tuple(diagnostics),
        )

    if req.kind == 'min_spaces':
        wanted = int(req.params.get('count', 1))
        spaces = [n for n in model.spatial_nodes if n.kind == 'space']
        if len(spaces) >= wanted:
            return IfcIntakeResult(
                requirement_id=req.requirement_id,
                verdict='satisfied',
            )
        diagnostics.append(
            f'missing_space: {len(spaces)} IfcSpace entities found, '
            f'{wanted} required'
        )
        return IfcIntakeResult(
            requirement_id=req.requirement_id,
            verdict='unsatisfied',
            diagnostics=tuple(diagnostics),
        )

    if req.kind == 'storey_context':
        storeys = [
            n for n in model.spatial_nodes if n.kind == 'storey'
        ]
        spaces_ok = all(
            n.parent_global_id
            for n in model.spatial_nodes
            if n.kind == 'space'
        )
        if storeys and spaces_ok:
            return IfcIntakeResult(
                requirement_id=req.requirement_id,
                verdict='satisfied',
            )
        if not storeys:
            diagnostics.append(
                'missing_storey: no IfcBuildingStorey in the '
                'aggregation tree'
            )
        if not spaces_ok:
            diagnostics.append(
                'missing_storey_context: at least one IfcSpace is not '
                'contained under a storey'
            )
        return IfcIntakeResult(
            requirement_id=req.requirement_id,
            verdict='unsatisfied',
            diagnostics=tuple(diagnostics),
        )

    if req.kind == 'geometric_representation':
        spaces = [
            e for e in model.elements if e.htdt_role == 'room_candidate'
        ]
        with_geom = [e for e in spaces if e.geometry_descriptors]
        if spaces and len(with_geom) == len(spaces):
            return IfcIntakeResult(
                requirement_id=req.requirement_id,
                verdict='satisfied',
            )
        if not spaces:
            diagnostics.append(
                'missing_space: no IfcSpace to carry geometry'
            )
        else:
            diagnostics.append(
                f'missing_geometry: {len(spaces) - len(with_geom)} '
                'IfcSpace entities have no extractable representation '
                '(supported: ExtrudedAreaSolid/FacetedBrep/BoundingBox)'
            )
        return IfcIntakeResult(
            requirement_id=req.requirement_id,
            verdict='unsatisfied',
            diagnostics=tuple(diagnostics),
        )

    if req.kind == 'opening_identity':
        openings = [
            e
            for e in model.elements
            if e.htdt_role == 'opening'
            and e.ifc_type != 'IFCOPENINGELEMENT'
        ]
        bad = [o for o in openings if not o.global_id]
        if not bad:
            return IfcIntakeResult(
                requirement_id=req.requirement_id,
                verdict='satisfied',
            )
        diagnostics.append(
            f'missing_identity: {len(bad)} door/window entities have no '
            'GlobalId; revision matching will be ambiguous'
        )
        return IfcIntakeResult(
            requirement_id=req.requirement_id,
            verdict='unsatisfied',
            diagnostics=tuple(diagnostics),
        )

    if req.kind == 'material_layers':
        boundaries = [
            e for e in model.elements if e.htdt_role == 'boundary'
        ]
        with_layers = [e for e in boundaries if e.material_layers]
        if boundaries and with_layers:
            return IfcIntakeResult(
                requirement_id=req.requirement_id,
                verdict='satisfied',
            )
        diagnostics.append(
            'missing_material_layers: no IfcMaterialLayerSet found on '
            'boundary elements; construction build-up stays unknown'
        )
        return IfcIntakeResult(
            requirement_id=req.requirement_id,
            verdict='unsatisfied',
            diagnostics=tuple(diagnostics),
        )

    if req.kind == 'adjacent_space_relations':
        count = model.coverage.relation_counts.get(
            'IFCRELSPACEBOUNDARY', 0
        )
        if count:
            return IfcIntakeResult(
                requirement_id=req.requirement_id,
                verdict='satisfied',
            )
        diagnostics.append(
            'missing_adjacency: no IfcRelSpaceBoundary relations found; '
            'adjacent-space topology unknown'
        )
        return IfcIntakeResult(
            requirement_id=req.requirement_id,
            verdict='unsatisfied',
            diagnostics=tuple(diagnostics),
        )

    return IfcIntakeResult(
        requirement_id=req.requirement_id,
        verdict='not_applicable',
        diagnostics=(f'unknown requirement kind {req.kind!r}',),
    )


def evaluate_ifc_intake(
    *,
    document_id: str,
    profile: IfcIntakeProfile,
    source: bytes | str,
    evaluated_at_utc: str,
) -> IfcIntakeEvaluation:
    """Evaluate an intake profile against raw STEP content.

    Runs before artifact construction so a failed intake still produces
    actionable diagnostics for the missing data (issue #578 §8).
    """
    raw = source.encode('utf-8') if isinstance(source, str) else bytes(source)
    file_sha = sha256(raw).hexdigest()
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise IfcStepParseError('STEP file is not valid UTF-8') from exc
    try:
        header, entities = parse_ifc_step(text)
        index = _Index(entities)
    except IfcStepParseError as exc:
        # Actionable diagnostics, not a generic parser failure (§8):
        # every requirement reports the same concrete reason.
        results = tuple(
            IfcIntakeResult(
                requirement_id=req.requirement_id,
                verdict='unsatisfied',
                diagnostics=(
                    f'model_resolution_failed: {exc}',
                ),
            )
            for req in profile.requirements
        )
        return _seal_intake_evaluation(
            document_id=document_id,
            profile=profile,
            file_sha256=file_sha,
            results=results,
            evaluated_at_utc=evaluated_at_utc,
        )
    try:
        model: IfcResolvedModel | None = resolve_ifc_model(header, entities)
    except IfcInteropError:
        model = None

    results = tuple(
        _eval_requirement(req, header, index, model)
        for req in profile.requirements
    )

    return _seal_intake_evaluation(
        document_id=document_id,
        profile=profile,
        file_sha256=file_sha,
        results=results,
        evaluated_at_utc=evaluated_at_utc,
    )


def _seal_intake_evaluation(
    *,
    document_id: str,
    profile: IfcIntakeProfile,
    file_sha256: str,
    results: Sequence[IfcIntakeResult],
    evaluated_at_utc: str,
) -> IfcIntakeEvaluation:
    if all(r.verdict == 'satisfied' for r in results):
        overall: IfcIntakeOverall = 'satisfied'
    elif any(
        r.verdict == 'unsatisfied'
        for r, req in zip(results, profile.requirements)
        if r.requirement_id == req.requirement_id and req.required
    ):
        overall = 'failed'
    else:
        overall = 'satisfied_with_gaps'

    probe = IfcIntakeEvaluation.model_construct(
        **_canon(
            IfcIntakeEvaluation,
            dict(
                evaluation_id='',
                document_id=document_id,
                profile_id=profile.profile_id,
                profile_sha256=profile.profile_sha256,
                artifact_id=None,
                artifact_sha256=None,
                file_sha256=file_sha256,
                results=tuple(results),
                overall_state=overall,
                evaluated_at_utc=evaluated_at_utc,
                evaluation_sha256='',
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return IfcIntakeEvaluation(
        **probe.model_dump(exclude={'evaluation_id', 'evaluation_sha256'}),
        evaluation_id=f'iie:{sha}',
        evaluation_sha256=sha,
    )


def default_htdt_intake_requirements() -> tuple[IfcIntakeRequirement, ...]:
    """The HTDT minimum-room intake profile from issue #578 §8."""
    return (
        IfcIntakeRequirement(
            requirement_id='schema',
            kind='schema_allowed',
            params={'allowed_identifiers': SUPPORTED_IFC_SCHEMA_IDENTIFIERS},
            description_ja='対応 IFC スキーマバージョンであること',
        ),
        IfcIntakeRequirement(
            requirement_id='units',
            kind='length_unit_declared',
            description_ja='長さ単位が宣言されていること',
        ),
        IfcIntakeRequirement(
            requirement_id='spaces',
            kind='min_spaces',
            params={'count': 1},
            description_ja='少なくとも1つの対象 IfcSpace が存在すること',
        ),
        IfcIntakeRequirement(
            requirement_id='storey',
            kind='storey_context',
            description_ja='空間が階コンテキストに含まれること',
        ),
        IfcIntakeRequirement(
            requirement_id='geometry',
            kind='geometric_representation',
            description_ja='対象空間に幾何表現が存在すること',
        ),
        IfcIntakeRequirement(
            requirement_id='openings',
            kind='opening_identity',
            required=False,
            description_ja='開口部要素が識別可能であること',
        ),
        IfcIntakeRequirement(
            requirement_id='materials',
            kind='material_layers',
            required=False,
            description_ja='境界要素に材料レイヤー構成が存在すること',
        ),
        IfcIntakeRequirement(
            requirement_id='adjacency',
            kind='adjacent_space_relations',
            required=False,
            description_ja='隣接空間関係が利用可能であること',
        ),
    )


# ---------------------------------------------------------------------------
# Export (issue #578 §9-§10) — bounded create-new STEP output
# ---------------------------------------------------------------------------


def _step_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _format_step_num(value: float) -> str:
    if value == int(value):
        return f'{value:.1f}'
    return repr(float(value))


_IFC_GID_ALPHABET = (
    '0123456789'
    'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    'abcdefghijklmnopqrstuvwxyz'
    '_$'
)


def _compress_global_id(raw: str) -> str:
    """Produce an IFC GlobalId-shaped token from a raw id string.

    A real GlobalId is 22 chars over ``0-9A-Za-z_$`` with a leading
    digit; preserved verbatim when the input already matches, else a
    deterministic 22-char token is derived from sha256.
    """
    if re.fullmatch(r'[0-9][0-9A-Za-z_$]{21}', raw):
        return raw
    value = int.from_bytes(sha256(raw.encode('utf-8')).digest()[:16], 'big')
    chars = [_IFC_GID_ALPHABET[value >> 126]]
    for k in range(20, -1, -1):
        chars.append(_IFC_GID_ALPHABET[(value >> (6 * k)) & 0x3F])
    return ''.join(chars)


def _emit_step(entities: list[tuple[str, str]]) -> str:
    lines = [
        'ISO-10303-21;',
        'HEADER;',
        "FILE_DESCRIPTION(('ViewDefinition [ReferenceView]'),'2;1');",
        (
            "FILE_NAME('htdt-export.ifc','',('htdt'),('htdt'),"
            "'htdt.ifc_step emitter','HTDT','');"
        ),
        "FILE_SCHEMA(('IFC4X3_ADD2'));",
        'ENDSEC;',
        'DATA;',
    ]
    for i, (name, args) in enumerate(entities, start=1):
        lines.append(f'#{i}={name}({args});')
    lines.append('ENDSEC;')
    lines.append('END-ISO-10303-21;')
    return '\n'.join(lines) + '\n'


def build_ifc_export_package(
    *,
    document_id: str,
    mode: IfcExportMode,
    label: str,
    spaces: Sequence[IfcEntityMapping],
    source_artifact: IfcImportArtifact | None,
    created_at_utc: str,
    provenance_note: str = 'HTDT bounded IFC export',
) -> IfcExportPackage:
    """Create a bounded coordination/proposal/as-built IFC export.

    Only entities carrying a normalized (metre) transform and
    extruded-area-solid geometry can be exported; everything else is
    listed in ``unexported_items`` with its reason (issue #578 §10).
    The source model is never mutated — this is create-new only.
    """
    entities: list[tuple[str, str]] = []
    unexported: list[str] = []

    def add(name: str, args: str) -> int:
        entities.append((name, args))
        return len(entities)

    origin = add('IFCCARTESIANPOINT', '(0.,0.,0.)')
    dir_z = add('IFCDIRECTION', '(0.,0.,1.)')
    dir_x = add('IFCDIRECTION', '(1.,0.,0.)')
    wcs = add('IFCAXIS2PLACEMENT3D', f'#{origin},#{dir_z},#{dir_x}')
    ctx = add(
        'IFCGEOMETRICREPRESENTATIONCONTEXT',
        f"$,'Model',3,1.E-05,#{wcs},$",
    )
    len_unit = add('IFCSIUNIT', '*,.LENGTHUNIT.,$,.METRE.')
    angle_unit = add('IFCSIUNIT', '*,.PLANEANGLEUNIT.,$,.RADIAN.')
    units = add('IFCUNITASSIGNMENT', f'(#{len_unit},#{angle_unit})')
    # IfcProject: GlobalId, OwnerHistory, Name, Description, ObjectType,
    # LongName, Phase, RepresentationContexts, UnitsInContext.
    project = add(
        'IFCPROJECT',
        f'{_step_quote(_compress_global_id("htdt-project"))},$,'
        f'{_step_quote(label)},$,$,$,$,(#{ctx}),#{units}',
    )
    site_lp = add('IFCLOCALPLACEMENT', f'$,#{wcs}')
    # IfcSite: +14 attrs (through SiteAddress).
    site = add(
        'IFCSITE',
        f'{_step_quote(_compress_global_id("htdt-site"))},$,'
        f'{_step_quote("Site")},$,$,#{site_lp},$,$,.ELEMENT.,$,$,$,$,$',
    )
    # IfcBuilding: 12 attrs.
    building = add(
        'IFCBUILDING',
        f'{_step_quote(_compress_global_id("htdt-building"))},$,'
        f'{_step_quote("Building")},$,$,#{site_lp},$,$,.ELEMENT.,$,$,$',
    )
    # IfcBuildingStorey: 12 attrs incl. Elevation.
    storey = add(
        'IFCBUILDINGSTOREY',
        f'{_step_quote(_compress_global_id("htdt-storey"))},$,'
        f'{_step_quote("Storey")},$,$,#{site_lp},$,$,.ELEMENT.,$,$,0.',
    )
    add(
        'IFCRELAGGREGATES',
        f'{_step_quote(_compress_global_id("agg-pb"))},$,$,$,#{project},'
        f'(#{site})',
    )
    add(
        'IFCRELAGGREGATES',
        f'{_step_quote(_compress_global_id("agg-sb"))},$,$,$,#{site},'
        f'(#{building})',
    )
    add(
        'IFCRELAGGREGATES',
        f'{_step_quote(_compress_global_id("agg-bs"))},$,$,$,#{building},'
        f'(#{storey})',
    )

    scale: float | None = None
    if (
        source_artifact is not None
        and source_artifact.coordinate.length_scale_to_meter is not None
    ):
        scale = source_artifact.coordinate.length_scale_to_meter

    exported_space_ids: list[int] = []
    mode_prop_value = {
        'reference_export': 'HTDT reference coordination shell',
        'update_proposal': 'HTDT proposed design update',
        'as_built_handoff': 'HTDT as-built handoff',
    }[mode]

    for mapping in spaces:
        if mapping.htdt_role != 'room_candidate':
            unexported.append(
                f'{mapping.ifc_type} '
                f'{mapping.ifc_global_id or mapping.step_entity_id}: '
                'non-space entities are not in the bounded export set'
            )
            continue
        if mapping.world_transform_m is None:
            unexported.append(
                f'space {mapping.ifc_global_id or mapping.step_entity_id}: '
                'no metre transform (undeclared source unit)'
            )
            continue
        extrusions = [
            g
            for g in mapping.geometry_descriptors
            if g.kind == 'extruded_area_solid' and g.profile_points
        ]
        if not extrusions:
            unexported.append(
                f'space {mapping.ifc_global_id or mapping.step_entity_id}: '
                'no extruded-area-solid geometry to export'
            )
            continue
        solid = extrusions[0]
        m = mapping.world_transform_m
        x_axis = (m[0], m[4], m[8])
        y_axis = (m[1], m[5], m[9])
        z_axis = _cross(x_axis, y_axis)
        loc = add(
            'IFCCARTESIANPOINT',
            '(%s,%s,%s)'
            % (
                _format_step_num(m[3]),
                _format_step_num(m[7]),
                _format_step_num(m[11]),
            ),
        )
        zdir = add(
            'IFCDIRECTION',
            '(%s,%s,%s)' % tuple(_format_step_num(v) for v in z_axis),
        )
        xdir = add(
            'IFCDIRECTION',
            '(%s,%s,%s)' % tuple(_format_step_num(v) for v in x_axis),
        )
        plac = add('IFCAXIS2PLACEMENT3D', f'#{loc},#{zdir},#{xdir}')
        lp = add('IFCLOCALPLACEMENT', f'#{site_lp},#{plac}')

        # Arbitrary closed profile via polyline, rescaled to metres.
        unit_scale = scale if scale is not None else 1.0
        pts_ids = []
        for px, py in solid.profile_points:
            pid = add(
                'IFCCARTESIANPOINT',
                f'({_format_step_num(px * unit_scale)},'
                f'{_format_step_num(py * unit_scale)})',
            )
            pts_ids.append(pid)
        polyline = add(
            'IFCPOLYLINE',
            '(' + ','.join(f'#{p}' for p in pts_ids) + ')',
        )
        profile = add(
            'IFCARBITRARYCLOSEDPROFILEDEF',
            f'.AREA.,$,#{polyline}',
        )
        edir = add('IFCDIRECTION', '(0.,0.,1.)')
        depth_m = (solid.depth_source_units or 0.0) * unit_scale
        extr = add(
            'IFCEXTRUDEDAREASOLID',
            f'#{profile},$,#{edir},{_format_step_num(depth_m)}',
        )
        shape = add(
            'IFCSHAPEREPRESENTATION',
            f"#{ctx},'Body','SweptSolid',(#{extr})",
        )
        pds = add('IFCPRODUCTDEFINITIONSHAPE', f'$,$,(#{shape})')
        gid = _compress_global_id(
            mapping.ifc_global_id or f'space-{mapping.step_entity_id}'
        )
        # IfcSpace: GlobalId, OwnerHistory, Name, Description, ObjectType,
        # ObjectPlacement, Representation, LongName, CompositionType,
        # InteriorOrExteriorSpace, ElevationWithFlooring (11 attrs).
        space = add(
            'IFCSPACE',
            f'{_step_quote(gid)},$,'
            f'{_step_quote(mapping.name or "Space")},$,$,#{lp},#{pds},'
            f'$,.ELEMENT.,.INTERNAL.,$',
        )
        exported_space_ids.append(space)

    if exported_space_ids:
        add(
            'IFCRELCONTAINEDINSPATIALSTRUCTURE',
            f'{_step_quote(_compress_global_id("cont"))},$,$,$,'
            '(' + ','.join(f'#{s}' for s in exported_space_ids) + f'),'
            f'#{storey}',
        )

    # Provenance pset — HTDT reference id + mode note (§9 bounded props).
    pval1 = add(
        'IFCPROPERTYSINGLEVALUE',
        f"'HTDTExportMode',$,IFCLABEL('{mode_prop_value}'),$",
    )
    pval2 = add(
        'IFCPROPERTYSINGLEVALUE',
        f"'HTDTDocumentId',$,IFCLABEL('{document_id}'),$",
    )
    src = source_artifact.artifact_id if source_artifact else 'none'
    pval3 = add(
        'IFCPROPERTYSINGLEVALUE',
        f"'HTDTSourceImport',$,IFCLABEL('{src}'),$",
    )
    pset = add(
        'IFCPROPERTYSET',
        f'{_step_quote(_compress_global_id("pset-htdt"))},$,'
        f"'Pset_HTDT_Provenance',$,(#{pval1},#{pval2},#{pval3})",
    )
    for space_id in exported_space_ids:
        add(
            'IFCRELDEFINESBYPROPERTIES',
            f'{_step_quote(_compress_global_id(f"rdp-{space_id}"))},$,$,$,'
            f'(#{space_id}),#{pset}',
        )

    step_text = _emit_step(entities)
    step_sha = sha256(step_text.encode('utf-8')).hexdigest()

    included = [
        'ifc4x3_add2_header',
        'project_units_metre',
        'spatial_skeleton_site_building_storey',
        'spaces_with_extruded_body',
        'pset_htdt_provenance',
    ]
    unexported.append(
        'scientific_arrays: RIR/FR evidence stays in HTDT artifacts '
        '(#578 §9)'
    )
    unexported.append(
        'ifc_material_layers: build-up names/thicknesses not exported in '
        'bounded mode'
    )

    probe = IfcExportPackage.model_construct(
        **_canon(
            IfcExportPackage,
            dict(
                export_id='',
                document_id=document_id,
                mode=mode,
                label=label,
                source_artifact_id=(
                    source_artifact.artifact_id if source_artifact else None
                ),
                source_artifact_sha256=(
                    source_artifact.artifact_sha256
                    if source_artifact
                    else None
                ),
                provenance_note=provenance_note,
                included_sections=tuple(included),
                unexported_items=tuple(unexported),
                step_text=step_text,
                step_sha256=step_sha,
                created_at_utc=created_at_utc,
                export_sha256='',
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return IfcExportPackage(
        **probe.model_dump(exclude={'export_id', 'export_sha256'}),
        export_id=f'ixp:{sha}',
        export_sha256=sha,
    )


__all__ = [
    'IFC_AXIS_CONVENTION',
    'IFC_INTEROP_AUTHORITY_VERSION',
    'IFC_INTEROP_SCHEMA_VERSION',
    'IFC_SEMANTIC_ROLES',
    'SUPPORTED_IFC_SCHEMA_IDENTIFIERS',
    'IfcCoordinateAuthority',
    'IfcDeltaChangeKind',
    'IfcDeltaItem',
    'IfcEntityMapping',
    'IfcExportMode',
    'IfcExportPackage',
    'IfcGeorefState',
    'IfcGeometryDescriptor',
    'IfcHtdtRole',
    'IfcImportArtifact',
    'IfcImportCoverage',
    'IfcIntakeEvaluation',
    'IfcIntakeOverall',
    'IfcIntakeProfile',
    'IfcIntakeRequirement',
    'IfcIntakeResult',
    'IfcIntakeVerdict',
    'IfcInteropError',
    'IfcMaterialLayerEvidence',
    'IfcOpeningLink',
    'IfcPropertyValue',
    'IfcReconciliationState',
    'IfcResolvedElement',
    'IfcResolvedModel',
    'IfcResolvedSpatialNode',
    'IfcStepParseError',
    'IfcUnitState',
    'build_ifc_export_package',
    'build_ifc_import',
    'build_ifc_intake_profile',
    'compute_ifc_revision_delta',
    'default_htdt_intake_requirements',
    'evaluate_ifc_intake',
    'mark_mapping_reconciliation',
    'resolve_ifc_model',
]
