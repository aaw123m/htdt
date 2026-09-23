"""Issue #669 regression tests: entity mesh import authority.

Source units, axis convention, scale and local anchor are explicit persisted
authority — nothing silently assumes HTDT meters/Z-up.
"""

from __future__ import annotations

import os
from hashlib import sha256

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from htdt.cad_scene import (
    EntityBodyGeometry,
    MeshImportAuthority,
    Offset3,
    Size3,
    mesh_body_envelope_state,
)
from htdt.mesh_import_authority import (
    MESH_IMPORT_AUTHORITY_VERSION,
    MeshImportCancelledError,
    apply_mesh_import_decision,
    format_declared_source_unit,
    import_entity_mesh_asset,
    legacy_mesh_import_authority,
    make_mesh_import_authority,
    mesh_asset_import_authority,
    mesh_import_axis_matrix,
    mesh_import_fit_state,
    mesh_unit_scale_to_meters,
    normalize_source_vertices,
    reconcile_mesh_import_authority,
)


_MILLIMETER_OBJ = b"""# 400mm x 300mm x 200mm tetra, mm coordinates
v 0 0 0
v 400 0 0
v 0 300 0
v 0 0 200
f 1 2 3
f 1 2 4
f 1 3 4
f 2 3 4
"""


def test_format_spec_units_and_scale_table() -> None:
    assert format_declared_source_unit('glb') == 'meters'
    assert format_declared_source_unit('htdt_meshbin_v1') == 'meters'
    assert format_declared_source_unit('obj') == 'unknown'
    assert format_declared_source_unit('stl') == 'unknown'


def test_authority_requires_concrete_operator_unit() -> None:
    with pytest.raises(ValueError, match='concrete unit'):
        MeshImportAuthority(
            source_unit='unknown',
            unit_declared_by='operator_confirmed',
            importer_id='t', importer_version='1',
        )
    with pytest.raises(ValueError, match='concrete unit'):
        MeshImportAuthority(
            source_unit='meters',
            unit_declared_by='undeclared',
            importer_id='t', importer_version='1',
        )
    with pytest.raises(ValueError, match='custom'):
        MeshImportAuthority(
            source_unit='custom',
            unit_declared_by='operator_confirmed',
            importer_id='t', importer_version='1',
        )
    with pytest.raises(ValueError, match='cannot be format_specification'):
        make_mesh_import_authority(
            source_unit='unknown', unit_declared_by='format_specification',
        )


def test_unit_scale_table() -> None:
    authority = make_mesh_import_authority(
        source_unit='millimeters', unit_declared_by='operator_confirmed',
    )
    assert mesh_unit_scale_to_meters(authority) == pytest.approx(0.001)
    custom = make_mesh_import_authority(
        source_unit='custom', unit_declared_by='operator_confirmed',
        custom_scale_to_meters=0.0254,
    )
    assert mesh_unit_scale_to_meters(custom) == pytest.approx(0.0254)


def test_axis_matrix_maps_y_up_forward_z_source() -> None:
    # A Y-up / Z-forward right-handed source: +Z_src (forward) → HTDT +Y,
    # +Y_src (up) → HTDT +Z, +X_src → HTDT +X? right = up × fwd = Y×Z = X → +X.
    matrix = mesh_import_axis_matrix('y+', 'z+', 'right')
    assert matrix is not None
    # Source point (0, 1, 0) (up) → (0, 0, 1)
    out = tuple(
        sum(matrix[r][c] * v for c, v in enumerate((0.0, 1.0, 0.0)))
        for r in range(3)
    )
    assert out == pytest.approx((0.0, 0.0, 1.0))
    # Unresolved conventions produce no matrix (coordinates pass through).
    assert mesh_import_axis_matrix('unknown', 'z+', 'right') is None
    assert mesh_import_axis_matrix('z+', 'z+', 'right') is None  # degenerate


def test_import_mm_obj_normalizes_to_meters() -> None:
    body, authority = import_entity_mesh_asset(
        _MILLIMETER_OBJ,
        source_name='tetra_mm.obj',
        source_unit='millimeters',
    )
    assert authority.source_unit == 'millimeters'
    assert authority.unit_declared_by == 'operator_confirmed'
    assert authority.importer_version == MESH_IMPORT_AUTHORITY_VERSION
    assert body.asset_sha256 == sha256(_MILLIMETER_OBJ).hexdigest()
    xs = [float(v.x_m) for v in body.vertices]
    assert max(xs) == pytest.approx(0.4)


def test_import_obj_requires_operator_unit() -> None:
    # OBJ with no declared unit and no format spec unit → must fail closed.
    with pytest.raises(ValueError):
        import_entity_mesh_asset(_MILLIMETER_OBJ, source_name='t.obj')


def test_import_glb_uses_format_spec_meters() -> None:
    # GLB declares meters per the glTF 2.0 spec — no operator input needed.
    import struct
    import json as _json

    positions = struct.pack('<9f', 0, 0, 0, 0.4, 0, 0, 0, 0.3, 0)
    positions += b'\x00' * ((4 - len(positions) % 4) % 4)
    indices = struct.pack('<3H', 0, 1, 2)
    indices += b'\x00' * ((4 - len(indices) % 4) % 4)
    bin_chunk = positions + indices
    json_doc = _json.dumps({
        'asset': {'version': '2.0'},
        'scene': 0,
        'scenes': [{'nodes': [0]}],
        'nodes': [{'mesh': 0}],
        'meshes': [{'primitives': [{'attributes': {'POSITION': 0}, 'indices': 1}]}],
        'bufferViews': [
            {'buffer': 0, 'byteOffset': 0, 'byteLength': 36},
            {'buffer': 0, 'byteOffset': 36, 'byteLength': 6},
        ],
        'accessors': [
            {'bufferView': 0, 'componentType': 5126, 'count': 3, 'type': 'VEC3'},
            {'bufferView': 1, 'componentType': 5123, 'count': 3, 'type': 'SCALAR'},
        ],
        'buffers': [{'byteLength': len(bin_chunk)}],
    }).encode('utf-8')
    json_doc += b' ' * ((4 - len(json_doc) % 4) % 4)
    glb = (
        b'glTF'
        + struct.pack('<II', 2, 12 + 8 + len(json_doc) + 8 + len(bin_chunk))
        + struct.pack('<I', len(json_doc)) + b'JSON' + json_doc
        + struct.pack('<I', len(bin_chunk)) + b'BIN\x00' + bin_chunk
    )
    body, authority = import_entity_mesh_asset(glb, source_name='t.glb')
    assert authority.source_unit == 'meters'
    assert authority.unit_declared_by == 'format_specification'
    assert max(float(v.x_m) for v in body.vertices) == pytest.approx(0.4)


def test_local_anchor_shifts_origin() -> None:
    # bottom_center anchor: entity origin lands under the mesh center.
    body, _ = import_entity_mesh_asset(
        _MILLIMETER_OBJ,
        source_name='t.obj',
        source_unit='millimeters',
        local_anchor='bottom_center',
    )
    minimum, maximum = body.transformed_bounds()
    assert minimum[0] == pytest.approx(-0.2)
    assert maximum[0] == pytest.approx(0.2)
    assert minimum[2] == pytest.approx(0.0)


def test_mesh_body_envelope_state_and_fit() -> None:
    body, _ = import_entity_mesh_asset(
        _MILLIMETER_OBJ, source_name='t.obj', source_unit='millimeters',
    )
    geometry = EntityBodyGeometry(kind='mesh_asset', mesh=body)
    assert mesh_body_envelope_state(geometry, Size3(x_m=1, y_m=1, z_m=1)) == 'contained'
    assert mesh_import_fit_state(geometry, Size3(x_m=1, y_m=1, z_m=1)) == 'fits_envelope'
    tight = Size3(x_m=0.2, y_m=0.2, z_m=0.2)
    assert mesh_import_fit_state(geometry, tight) == 'exceeds_envelope'
    # Oversize is never silently adopted.
    with pytest.raises(MeshImportCancelledError):
        apply_mesh_import_decision(geometry, tight, decision='cancel')
    # Rescale keeps the declared envelope, shrinking the mesh.
    size, rescaled = apply_mesh_import_decision(geometry, tight, decision='rescale')
    assert rescaled.mesh is not None
    assert rescaled.mesh.uniform_scale < 1.0
    assert mesh_import_fit_state(rescaled, size) == 'fits_envelope'


def test_legacy_mesh_authority_is_honest() -> None:
    authority = legacy_mesh_import_authority()
    assert authority.unit_declared_by == 'legacy_assumed_meter'
    assert authority.source_unit == 'unknown'
    # A body with no recorded authority resolves to the synthesized legacy one.
    body, _ = import_entity_mesh_asset(
        _MILLIMETER_OBJ, source_name='t.obj', source_unit='millimeters',
    )
    stripped = body.model_copy(update={'import_authority': None})
    geometry = EntityBodyGeometry(kind='mesh_asset', mesh=stripped)
    resolved = mesh_asset_import_authority(geometry)
    assert resolved is not None
    assert resolved.unit_declared_by == 'legacy_assumed_meter'
    # Non-mesh bodies carry no import authority at all.
    box = EntityBodyGeometry(kind='box')
    assert mesh_asset_import_authority(box) is None


def test_reconcile_reinterprets_legacy_import() -> None:
    # A legacy import whose coordinates were actually millimeters.
    body, _ = import_entity_mesh_asset(
        _MILLIMETER_OBJ,
        source_name='t.obj',
        source_unit='meters',   # what the old path assumed
        local_anchor='source_origin',
    )
    stripped = body.model_copy(update={'import_authority': None})
    geometry = EntityBodyGeometry(kind='mesh_asset', mesh=stripped)
    # size_m must cover the assumed-meter extent (huge) — then reconcile.
    size, reconciled = reconcile_mesh_import_authority(
        geometry, Size3(x_m=800, y_m=800, z_m=800),
        source_unit='millimeters',
    )
    new_body = reconciled.mesh
    assert new_body is not None
    assert new_body.import_authority is not None
    assert new_body.import_authority.unit_declared_by == 'operator_confirmed'
    assert new_body.import_authority.source_unit == 'millimeters'
    assert max(float(v.x_m) for v in new_body.vertices) == pytest.approx(0.4)
    assert mesh_import_fit_state(reconciled, size) == 'fits_envelope'


def test_normalize_source_vertices_identity() -> None:
    authority = make_mesh_import_authority(
        source_unit='meters', unit_declared_by='operator_confirmed',
    )
    vertices = normalize_source_vertices([(0.0, 0.0, 0.0), (1.0, 2.0, 3.0)], authority)
    assert [(v.x_m, v.y_m, v.z_m) for v in vertices] == [
        (0.0, 0.0, 0.0), (1.0, 2.0, 3.0),
    ]
