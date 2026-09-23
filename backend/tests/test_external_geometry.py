"""Issue #525 regression tests: external 3D geometry import.

OBJ/PLY/STL/GLB ingest as provenance-preserving reference meshes that keep
source coordinates and honest (possibly undeclared) import authority until
explicit semantic promotion.
"""

from __future__ import annotations

import os
import struct
from hashlib import sha256
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import BodyMeshVertex
from htdt.external_geometry import (
    EXTERNAL_REFERENCE_STATE,
    MeshPromotionError,
    deserialize_external_reference_mesh,
    import_external_reference_mesh,
    promote_reference_mesh_to_body,
    reference_mesh_blob_digests,
    serialize_external_reference_mesh,
)
from htdt.mesh_import_authority import (
    make_mesh_import_authority,
)
from htdt.raw_mesh import RawMeshImportError, import_raw_visual_mesh


_TETRA_OBJ = b"""v 0.0 0.0 0.0
v 0.4 0.0 0.0
v 0.0 0.3 0.0
v 0.0 0.0 0.2
f 1 2 3
f 1 2 4
f 1 3 4
f 2 3 4
"""

_TETRA_PLY = b"""ply
format ascii 1.0
element vertex 4
property float x
property float y
property float z
element face 4
property list uchar int vertex_indices
end_header
0.0 0.0 0.0
0.4 0.0 0.0
0.0 0.3 0.0
0.0 0.0 0.2
3 0 1 2
3 0 1 3
3 0 2 3
3 1 2 3
"""


def _tetra_binary_stl() -> bytes:
    triangles = [
        ((0, 0, 0), (0.4, 0, 0), (0, 0.3, 0)),
        ((0, 0, 0), (0.4, 0, 0), (0, 0, 0.2)),
        ((0, 0, 0), (0, 0.3, 0), (0, 0, 0.2)),
        ((0.4, 0, 0), (0, 0.3, 0), (0, 0, 0.2)),
    ]
    payload = bytearray(b'\x00' * 80)
    payload += struct.pack('<I', len(triangles))
    for tri in triangles:
        payload += struct.pack('<fff', 0.0, 0.0, 1.0)  # normal (unused)
        for vertex in tri:
            payload += struct.pack('<fff', *vertex)
        payload += struct.pack('<H', 0)
    return bytes(payload)


_TETRA_STL_ASCII = b"""solid tetra
facet normal 0 0 1
outer loop
vertex 0.0 0.0 0.0
vertex 0.4 0.0 0.0
vertex 0.0 0.3 0.0
endloop
endfacet
endsolid tetra
"""


def _store(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def test_import_reference_obj_undeclared_units(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    reference = import_external_reference_mesh(
        _TETRA_OBJ, source_name='tetra.obj', store_blob=repository.store_blob,
    )
    assert reference.semantic_state == EXTERNAL_REFERENCE_STATE
    assert reference.source_format == 'obj'
    assert reference.original_asset_sha256 == sha256(_TETRA_OBJ).hexdigest()
    assert reference.vertex_count == 4
    assert reference.triangle_count == 4
    # Units are honestly undeclared; bounds keep source coordinates.
    assert reference.import_authority.unit_declared_by == 'undeclared'
    assert float(reference.bounds_max.x_m) == pytest.approx(0.4)
    # The blob it binds exists in the store.
    assert repository.read_blob(reference.geometry_blob_sha256) is not None
    assert reference_mesh_blob_digests(reference) == frozenset(
        {reference.geometry_blob_sha256}
    )
    # Content-derived identity is stable.
    again = import_external_reference_mesh(
        _TETRA_OBJ, source_name='tetra.obj', store_blob=repository.store_blob,
    )
    assert again.reference_id == reference.reference_id


def test_import_reference_ply_and_stl(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    ply = import_external_reference_mesh(
        _TETRA_PLY, source_name='tetra.ply', store_blob=repository.store_blob,
    )
    assert ply.source_format == 'ply'
    assert ply.vertex_count == 4
    stl = import_external_reference_mesh(
        _tetra_binary_stl(), source_name='tetra.stl',
        store_blob=repository.store_blob,
    )
    assert stl.source_format == 'stl'
    assert stl.vertex_count == 4


def test_raw_mesh_parses_ascii_and_binary_stl_and_ply() -> None:
    ascii_mesh = import_raw_visual_mesh(_TETRA_STL_ASCII, source_name='t.stl')
    assert ascii_mesh.provenance.asset_format == 'stl'
    assert len(ascii_mesh.triangles) == 1
    binary_mesh = import_raw_visual_mesh(_tetra_binary_stl(), source_name='t.stl')
    assert binary_mesh.provenance.asset_format == 'stl'
    assert len(binary_mesh.triangles) == 4
    ply_mesh = import_raw_visual_mesh(_TETRA_PLY, source_name='t.ply')
    assert ply_mesh.provenance.asset_format == 'ply'
    assert len(ply_mesh.vertices) == 4


def test_promotion_requires_declared_units(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    reference = import_external_reference_mesh(
        _TETRA_OBJ, source_name='t.obj', store_blob=repository.store_blob,
    )
    # Undeclared authority cannot be silently promoted.
    with pytest.raises(MeshPromotionError, match='declared source unit'):
        promote_reference_mesh_to_body(reference, repository.read_blob)


def test_promotion_with_declared_units_normalizes(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    # Operator-declared millimeters source.
    reference = import_external_reference_mesh(
        b"""v 0 0 0
v 400 0 0
v 0 300 0
v 0 0 200
f 1 2 3
f 1 2 4
f 1 3 4
f 2 3 4
""",
        source_name='t_mm.obj',
        store_blob=repository.store_blob,
        source_unit='millimeters',
        unit_declared_by='operator_confirmed',
    )
    body = promote_reference_mesh_to_body(reference, repository.read_blob)
    assert body.asset_sha256 == reference.original_asset_sha256
    assert body.import_authority is not None
    assert body.import_authority.source_unit == 'millimeters'
    assert max(float(v.x_m) for v in body.vertices) == pytest.approx(0.4)


def test_promotion_fails_closed_on_missing_blob(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    reference = import_external_reference_mesh(
        _TETRA_OBJ, source_name='t.obj', store_blob=repository.store_blob,
        source_unit='meters', unit_declared_by='operator_confirmed',
    )
    with pytest.raises(MeshPromotionError, match='missing'):
        promote_reference_mesh_to_body(reference, lambda _sha: None)
    with pytest.raises(MeshPromotionError, match='digest'):
        promote_reference_mesh_to_body(reference, lambda _sha: b'x')


def test_reference_serialization_round_trip(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    reference = import_external_reference_mesh(
        _TETRA_OBJ, source_name='t.obj', store_blob=repository.store_blob,
    )
    reopened = deserialize_external_reference_mesh(
        serialize_external_reference_mesh(reference)
    )
    assert reopened == reference
