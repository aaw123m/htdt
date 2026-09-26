"""Entity mesh import authority (Issue #669).

Imported mesh vertex coordinates are *source* coordinates — OBJ carries no
units at all, GLB declares meters by spec, STL/PLY are unitless, and capture
pipelines export millimeters. Previously ``RoomWorkspaceController.
attach_mesh_asset`` copied parsed coordinates into ``size_m`` space as if the
source were already HTDT meters/Z-up, silently baking an assumption nobody
recorded.

This module makes the interpretation explicit and persisted:

- :class:`MeshImportAuthority` (in ``cad_scene``) records the source unit, who
  declared it, the axis convention, the local anchor, and the importer — it is
  stored on ``BodyMeshAsset.import_authority`` / ``BodyMeshReference.
  import_authority`` so every later reader can inspect what made the
  entity-local meter geometry authoritative.
- :func:`import_entity_mesh_asset` normalizes source coordinates to meters:
  ``v_local = R_axis · v_source · unit_scale − anchor_shift``; the stored
  vertices are normalized meters (``uniform_scale`` stays 1.0), so identical
  geometry under different declared units content-deduplicates correctly.
- Pre-authority meshes carry no recorded authority. They stay readable, but
  :func:`mesh_asset_import_authority` surfaces them as
  ``legacy_assumed_meter`` — they are never silently reinterpreted as a
  freshly declared unit.
- An oversized import never silently adopts a bounding envelope: the caller
  must pass an explicit ``MeshImportOversizeDecision`` (adopt the true bounds,
  rescale, or cancel).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Sequence

if TYPE_CHECKING:
    from .raw_mesh_repair import RepairedRawMesh

from .cad_scene import (
    BodyMeshAsset,
    BodyMeshTriangle,
    BodyMeshVertex,
    EntityBodyGeometry,
    MeshImportAuthority,
    Offset3,
    Size3,
    mesh_body_envelope_state,
)
from .raw_mesh import (
    RAW_MESH_IMPORTER_ID,
    RawMeshFormat,
    import_raw_visual_mesh,
)


MESH_IMPORT_AUTHORITY_VERSION: Literal['2'] = '2'

# Declared source-unit scale factors (to meters). ``custom``/``unknown`` have
# no declared scale: custom resolves from ``custom_scale_to_meters``; unknown
# never reaches an operator-confirmed authority (validated on the model).
_UNIT_SCALE_TO_METERS = {
    'meters': 1.0,
    'millimeters': 0.001,
    'centimeters': 0.01,
    'inches': 0.0254,
    'feet': 0.3048,
}

# Unit scales that can be derived without operator input: GLB declares meters
# per the glTF 2.0 specification; the HTDTMSH1 normalized blob format is
# meters by construction. OBJ/PLY/STL carry no unit declaration and must be
# operator-confirmed.
_FORMAT_SPEC_UNITS: dict[str, str] = {
    'glb': 'meters',
    'htdt_meshbin_v1': 'meters',
}

MeshImportOversizeDecision = Literal['adopt', 'rescale', 'cancel']

_AXIS_UNIT_VECTORS = {
    'x+': (1.0, 0.0, 0.0),
    'x-': (-1.0, 0.0, 0.0),
    'y+': (0.0, 1.0, 0.0),
    'y-': (0.0, -1.0, 0.0),
    'z+': (0.0, 0.0, 1.0),
    'z-': (0.0, 0.0, -1.0),
}


class MeshImportCancelledError(ValueError):
    """An oversized mesh import was cancelled by explicit operator decision."""


def mesh_unit_scale_to_meters(authority: MeshImportAuthority) -> float:
    """The unit scale a source coordinate needs to become entity-local meters."""

    if authority.source_unit == 'custom':
        assert authority.custom_scale_to_meters is not None  # validator
        return float(authority.custom_scale_to_meters)
    if authority.source_unit == 'unknown':
        # Only reachable for legacy_assumed_meter provenance, whose decode-time
        # assumption was exactly "source coordinates are already meters".
        return 1.0
    return _UNIT_SCALE_TO_METERS[authority.source_unit]


def _cross(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def mesh_import_axis_matrix(
    up_axis: str,
    forward_axis: str,
    handedness: str,
) -> tuple[tuple[float, float, float, float], ...] | None:
    """Rotation mapping source coordinates onto HTDT axes (+Y fwd, +Z up).

    Returns ``None`` when the declared convention cannot be resolved to a
    unique orthogonal basis (unknown or degenerate axes); callers then keep
    source coordinates unrotated and the authority records the unresolved
    convention honestly.
    """

    if handedness not in ('right', 'left'):
        return None
    up = _AXIS_UNIT_VECTORS.get(up_axis)
    forward = _AXIS_UNIT_VECTORS.get(forward_axis)
    if up is None or forward is None:
        return None
    if abs(sum(u * f for u, f in zip(up, forward))) > 1e-9:
        return None  # up and forward must be orthogonal

    # Source basis (columns of the source→HTDT mapping, in source coords):
    # right = forward × up for a right-handed source (matching HTDT's own
    # right-handed X×Y=Z convention); flipped for a left-handed source.
    right = _cross(forward, up)
    if handedness == 'left':
        right = (-right[0], -right[1], -right[2])

    # HTDT target basis: X right, Y forward (rear), Z up.
    # R maps source right→(1,0,0), forward→(0,1,0), up→(0,0,1).
    basis = (
        (right[0], forward[0], up[0]),
        (right[1], forward[1], up[1]),
        (right[2], forward[2], up[2]),
    )
    # basis is orthogonal → transpose is the inverse rotation.
    return (
        (basis[0][0], basis[1][0], basis[2][0]),
        (basis[0][1], basis[1][1], basis[2][1]),
        (basis[0][2], basis[1][2], basis[2][2]),
    )


def format_declared_source_unit(
    asset_format: RawMeshFormat,
) -> Literal['meters', 'unknown']:
    """The unit the source *format itself* declares, or ``unknown``."""

    return 'meters' if asset_format in _FORMAT_SPEC_UNITS else 'unknown'  # type: ignore[return-value]


def _apply_matrix(
    matrix: tuple[tuple[float, float, float, float], ...],
    point: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        matrix[0][0] * point[0] + matrix[0][1] * point[1] + matrix[0][2] * point[2],
        matrix[1][0] * point[0] + matrix[1][1] * point[1] + matrix[1][2] * point[2],
        matrix[2][0] * point[0] + matrix[2][1] * point[1] + matrix[2][2] * point[2],
    )


def _anchor_shift(
    anchor: str,
    vertices_m: list[tuple[float, float, float]],
    anchor_offset_m: Offset3 | None,
) -> tuple[float, float, float]:
    """Translation that places the declared local anchor at the entity origin."""

    if anchor in ('source_origin', 'equipment_reference'):
        return (0.0, 0.0, 0.0)
    if anchor == 'custom_offset':
        assert anchor_offset_m is not None  # validator
        return (-anchor_offset_m.x_m, -anchor_offset_m.y_m, -anchor_offset_m.z_m)
    xs = [v[0] for v in vertices_m]
    ys = [v[1] for v in vertices_m]
    zs = [v[2] for v in vertices_m]
    center_x = (min(xs) + max(xs)) * 0.5
    center_y = (min(ys) + max(ys)) * 0.5
    if anchor == 'bounds_center':
        center_z = (min(zs) + max(zs)) * 0.5
    else:  # bottom_center
        center_z = min(zs)
    return (-center_x, -center_y, -center_z)


def make_mesh_import_authority(
    *,
    source_unit: str,
    unit_declared_by: str,
    custom_scale_to_meters: float | None = None,
    up_axis: str = 'unknown',
    forward_axis: str = 'unknown',
    handedness: str = 'unknown',
    local_anchor: str = 'source_origin',
    anchor_offset_m: Offset3 | None = None,
    importer_id: str = RAW_MESH_IMPORTER_ID,
    importer_version: str = MESH_IMPORT_AUTHORITY_VERSION,
) -> MeshImportAuthority:
    """Construct a validated import-authority record.

    ``unit_declared_by='format_specification'`` is only accepted when the
    format truly declares a unit (GLB/HTDTMSH1 → meters); everything else
    must be operator-confirmed.
    """

    if unit_declared_by == 'format_specification' and source_unit == 'unknown':
        raise ValueError('a format that declares no unit cannot be format_specification')
    return MeshImportAuthority(
        source_unit=source_unit,  # type: ignore[arg-type]
        unit_declared_by=unit_declared_by,  # type: ignore[arg-type]
        custom_scale_to_meters=custom_scale_to_meters,
        source_up_axis=up_axis,  # type: ignore[arg-type]
        source_forward_axis=forward_axis,  # type: ignore[arg-type]
        handedness=handedness,  # type: ignore[arg-type]
        convention_declared_by=(
            'format_specification'
            if unit_declared_by == 'format_specification'
            else (
                'undeclared'
                if unit_declared_by == 'undeclared'
                else 'operator_confirmed'
            )
        ),
        local_anchor=local_anchor,  # type: ignore[arg-type]
        anchor_offset_m=anchor_offset_m,
        importer_id=importer_id,
        importer_version=importer_version,
    )


def normalize_source_vertices(
    source_vertices: Sequence[tuple[float, float, float]],
    authority: MeshImportAuthority,
) -> tuple[BodyMeshVertex, ...]:
    """Apply unit scale, axis convention, and local anchor to source vertices.

    Produces entity-local *normalized meters*: identical source geometry
    declared under equivalent units normalizes to identical vertices, so
    content-addressed deduplication works across import declarations.
    """

    scale = mesh_unit_scale_to_meters(authority)
    rotation = mesh_import_axis_matrix(
        authority.source_up_axis,
        authority.source_forward_axis,
        authority.handedness,
    )
    scaled = [(v[0] * scale, v[1] * scale, v[2] * scale) for v in source_vertices]
    rotated = (
        [_apply_matrix(rotation, v) for v in scaled] if rotation is not None else scaled
    )
    shift = _anchor_shift(authority.local_anchor, rotated, authority.anchor_offset_m)
    return tuple(
        BodyMeshVertex(x_m=v[0] + shift[0], y_m=v[1] + shift[1], z_m=v[2] + shift[2])
        for v in rotated
    )


def import_entity_mesh_asset(
    asset: bytes,
    *,
    source_name: str,
    source_unit: str | None = None,
    unit_declared_by: str | None = None,
    custom_scale_to_meters: float | None = None,
    up_axis: str = 'unknown',
    forward_axis: str = 'unknown',
    handedness: str = 'unknown',
    local_anchor: str = 'source_origin',
    anchor_offset_m: Offset3 | None = None,
    format_hint: RawMeshFormat | None = None,
    repaired_mesh: RepairedRawMesh | None = None,
) -> tuple[BodyMeshAsset, MeshImportAuthority]:
    """Import bytes into a normalized meter mesh asset with recorded authority.

    Units are never assumed: when the format declares units (GLB), the spec
    value is used and recorded as ``format_specification``; when it does not
    (OBJ and any other unitless source), the caller must pass an explicit
    ``source_unit`` — recorded as ``operator_confirmed``. Declaring
    ``source_unit='unknown'`` under ``operator_confirmed`` is rejected.

    ``repaired_mesh`` (a ``RepairedRawMesh`` produced by
    ``raw_mesh_repair.apply_raw_mesh_repair`` against this same source)
    substitutes its bounded-repair geometry for the parsed source — the
    asset keeps the original bytes' provenance while its vertices come from
    the operator-previewed repair.
    """

    imported = import_raw_visual_mesh(asset, source_name=source_name, format_hint=format_hint)
    source_vertices = imported.vertices
    source_triangles = imported.triangles
    if repaired_mesh is not None:
        source_vertices = repaired_mesh.vertices
        source_triangles = repaired_mesh.triangles
    spec_unit = format_declared_source_unit(imported.provenance.asset_format)
    if source_unit is None:
        source_unit = spec_unit
    if unit_declared_by is None:
        unit_declared_by = (
            'format_specification' if spec_unit != 'unknown' else 'operator_confirmed'
        )
    authority = make_mesh_import_authority(
        source_unit=source_unit,
        unit_declared_by=unit_declared_by,
        custom_scale_to_meters=custom_scale_to_meters,
        up_axis=up_axis,
        forward_axis=forward_axis,
        handedness=handedness,
        local_anchor=local_anchor,
        anchor_offset_m=anchor_offset_m,
        importer_version=MESH_IMPORT_AUTHORITY_VERSION,
    )
    vertices = normalize_source_vertices(
        [(v.x, v.y, v.z) for v in source_vertices],
        authority,
    )
    body = BodyMeshAsset(
        asset_sha256=imported.provenance.original_asset_sha256,
        source_name=imported.provenance.source_name,
        asset_format=imported.provenance.asset_format,
        original_size_bytes=imported.provenance.original_size_bytes,
        vertices=vertices,
        triangles=tuple(
            BodyMeshTriangle(a=t.a, b=t.b, c=t.c) for t in source_triangles
        ),
        import_authority=authority,
    )
    return body, authority


def mesh_import_scene_transform(
    authority: MeshImportAuthority,
    source_vertices: Sequence[tuple[float, float, float]],
) -> tuple[tuple[float, float, float, float], ...]:
    """4×4 source→scene-metre affine identical to ``normalize_source_vertices``.

    The entity-body path normalizes vertices imperatively; the semantic
    conversion path (``SemanticCoordinateTransform``) needs the same
    interpretation expressed as a matrix. Both derive from the same helpers,
    so the two paths cannot drift apart.
    """

    scale = mesh_unit_scale_to_meters(authority)
    rotation = mesh_import_axis_matrix(
        authority.source_up_axis,
        authority.source_forward_axis,
        authority.handedness,
    )
    identity = (
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
    )
    basis = rotation if rotation is not None else identity
    rotated = [
        _apply_matrix(basis, (v[0] * scale, v[1] * scale, v[2] * scale))
        for v in source_vertices
    ]
    shift = _anchor_shift(authority.local_anchor, rotated, authority.anchor_offset_m)
    return (
        (basis[0][0] * scale, basis[0][1] * scale, basis[0][2] * scale, shift[0]),
        (basis[1][0] * scale, basis[1][1] * scale, basis[1][2] * scale, shift[1]),
        (basis[2][0] * scale, basis[2][1] * scale, basis[2][2] * scale, shift[2]),
        (0.0, 0.0, 0.0, 1.0),
    )


def legacy_mesh_import_authority(mesh: BodyMeshAsset | None = None) -> MeshImportAuthority:
    """The synthesized provenance of a pre-authority-v2 inline mesh.

    Legacy ``attach_mesh_asset`` copies decoded coordinates straight into
    ``size_m`` space — i.e. it *assumed* meters. That assumption is recorded
    honestly so the body is never mistaken for an operator-declared unit.
    """

    return MeshImportAuthority(
        source_unit='unknown',
        unit_declared_by='legacy_assumed_meter',
        source_up_axis='unknown',
        source_forward_axis='unknown',
        handedness='unknown',
        convention_declared_by='legacy_assumed',
        local_anchor='source_origin',
        importer_id=RAW_MESH_IMPORTER_ID,
        importer_version='1',
    )


def mesh_asset_import_authority(
    body_geometry: EntityBodyGeometry | None,
) -> MeshImportAuthority | None:
    """The recorded import authority of a mesh body (or synthesized legacy)."""

    if body_geometry is None or body_geometry.kind != 'mesh_asset':
        return None
    recorded = (
        body_geometry.mesh_reference.import_authority
        if body_geometry.mesh is None
        else body_geometry.mesh.import_authority
    )
    return recorded if recorded is not None else legacy_mesh_import_authority(
        body_geometry.mesh
    )


def mesh_import_fit_state(
    body_geometry: EntityBodyGeometry,
    size_m: Size3,
) -> Literal['fits_envelope', 'exceeds_envelope', 'not_applicable']:
    """Whether a mesh body's transformed bounds fit the declared size_m envelope."""

    state = mesh_body_envelope_state(body_geometry, size_m)
    if state == 'contained':
        return 'fits_envelope'
    if state == 'envelope_unverified':
        return 'exceeds_envelope'
    return 'not_applicable'


def apply_mesh_import_decision(
    body_geometry: EntityBodyGeometry,
    size_m: Size3,
    *,
    decision: MeshImportOversizeDecision,
) -> tuple[Size3, EntityBodyGeometry]:
    """Resolve an oversized mesh import by explicit operator decision.

    ``adopt`` grows ``size_m`` to the mesh's true transformed bounds (the
    envelope stays a truthful superset). ``rescale`` scales the mesh down so
    it fits the declared envelope, keeping ``size_m`` as authored. ``cancel``
    raises — no geometry is adopted and no envelope is silently trusted.
    """

    if mesh_import_fit_state(body_geometry, size_m) != 'exceeds_envelope':
        return size_m, body_geometry
    if decision == 'cancel':
        raise MeshImportCancelledError('mesh import cancelled: body exceeds size_m envelope')
    bounds = body_geometry.mesh_bounds_m()
    assert bounds is not None
    minimum, maximum = bounds
    half = (float(size_m.x_m) * 0.5, float(size_m.y_m) * 0.5, float(size_m.z_m) * 0.5)
    if decision == 'adopt':
        return (
            Size3(
                x_m=2.0 * max(half[0], abs(minimum[0]), abs(maximum[0])),
                y_m=2.0 * max(half[1], abs(minimum[1]), abs(maximum[1])),
                z_m=2.0 * max(half[2], abs(minimum[2]), abs(maximum[2])),
            ),
            body_geometry,
        )
    # rescale
    scale = 1.0
    for axis in range(3):
        extent = max(abs(minimum[axis]), abs(maximum[axis]))
        if extent > half[axis] and extent > 0:
            scale = min(scale, half[axis] / extent)
    if body_geometry.mesh is not None:
        mesh = body_geometry.mesh.model_copy(
            update={'uniform_scale': body_geometry.mesh.uniform_scale * scale}
        )
        return size_m, body_geometry.model_copy(update={'mesh': mesh})
    reference = body_geometry.mesh_reference
    assert reference is not None
    updated = reference.model_copy(
        update={
            'uniform_scale': reference.uniform_scale * scale,
            'bounds_min_m': type(reference.bounds_min_m)(
                x_m=minimum[0] * scale,
                y_m=minimum[1] * scale,
                z_m=minimum[2] * scale,
            ),
            'bounds_max_m': type(reference.bounds_max_m)(
                x_m=maximum[0] * scale,
                y_m=maximum[1] * scale,
                z_m=maximum[2] * scale,
            ),
        }
    )
    return size_m, body_geometry.model_copy(update={'mesh_reference': updated})


def reconcile_mesh_import_authority(
    body_geometry: EntityBodyGeometry,
    size_m: Size3,
    *,
    source_unit: str,
    custom_scale_to_meters: float | None = None,
    up_axis: str | None = None,
    forward_axis: str | None = None,
    handedness: str | None = None,
    local_anchor: str | None = None,
    anchor_offset_m: Offset3 | None = None,
    decision: MeshImportOversizeDecision = 'cancel',
) -> tuple[Size3, EntityBodyGeometry]:
    """Operator reconciliation of a legacy (or re-declared) mesh import.

    Re-interprets the mesh under the operator-confirmed declaration: vertices
    are rescaled by the ratio of the newly declared unit to the recorded one,
    the local anchor is re-applied, and the result must still resolve the
    envelope decision explicitly. The returned body carries
    ``unit_declared_by='operator_confirmed'`` — the upgrade is recorded, not
    silent.
    """

    mesh = body_geometry.mesh
    if body_geometry.kind != 'mesh_asset' or mesh is None:
        raise ValueError('mesh import reconciliation requires a resolved mesh body')
    current = mesh.import_authority or legacy_mesh_import_authority(mesh)
    old_scale = mesh_unit_scale_to_meters(current)
    new_authority = make_mesh_import_authority(
        source_unit=source_unit,
        unit_declared_by='operator_confirmed',
        custom_scale_to_meters=custom_scale_to_meters,
        up_axis=up_axis if up_axis is not None else current.source_up_axis,
        forward_axis=(
            forward_axis if forward_axis is not None else current.source_forward_axis
        ),
        handedness=handedness if handedness is not None else current.handedness,
        local_anchor=local_anchor if local_anchor is not None else current.local_anchor,
        anchor_offset_m=anchor_offset_m if anchor_offset_m is not None else current.anchor_offset_m,
        importer_version=MESH_IMPORT_AUTHORITY_VERSION,
    )
    ratio = mesh_unit_scale_to_meters(new_authority) / old_scale
    rotation = mesh_import_axis_matrix(
        new_authority.source_up_axis,
        new_authority.source_forward_axis,
        new_authority.handedness,
    )
    # Vertices were normalized under the old authority's axis convention;
    # re-derive source-space by undoing its scale, then reapply the new
    # interpretation. (Undo of the old rotation is only possible when both
    # conventions resolve to a matrix; otherwise the source convention was
    # recorded unknown and coordinates pass through unrotated, matching how
    # they were originally stored.)
    rescaled = [
        (v.x_m * ratio, v.y_m * ratio, v.z_m * ratio) for v in mesh.vertices
    ]
    if rotation is not None:
        rescaled = [_apply_matrix(rotation, v) for v in rescaled]
    shift = _anchor_shift(new_authority.local_anchor, rescaled, new_authority.anchor_offset_m)
    updated_mesh = mesh.model_copy(
        update={
            'vertices': tuple(
                BodyMeshVertex(x_m=v[0] + shift[0], y_m=v[1] + shift[1], z_m=v[2] + shift[2])
                for v in rescaled
            ),
            'import_authority': new_authority,
        }
    )
    updated_body = body_geometry.model_copy(
        update={'mesh': updated_mesh, 'mesh_reference': None}
    )
    return apply_mesh_import_decision(updated_body, size_m, decision=decision)
