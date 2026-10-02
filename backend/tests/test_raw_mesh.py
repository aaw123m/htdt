import json
from pathlib import Path
import struct

import pytest

from htdt.raw_mesh import (
    RawMeshDiagnosticProfile,
    RawMeshImportError,
    deserialize_raw_mesh_diagnostics,
    deserialize_raw_visual_mesh,
    diagnose_raw_visual_mesh,
    import_raw_visual_mesh,
    serialize_raw_mesh_diagnostics,
    serialize_raw_visual_mesh,
)


def _minimal_glb() -> bytes:
    positions = struct.pack('<9f', 0, 0, 0, 1, 0, 0, 0, 1, 0)
    indices = struct.pack('<3H', 0, 1, 2)
    binary = positions + indices
    binary += b'\x00' * ((-len(binary)) % 4)
    document = {
        'asset': {'version': '2.0'},
        'buffers': [{'byteLength': len(binary)}],
        'bufferViews': [
            {'buffer': 0, 'byteOffset': 0, 'byteLength': len(positions)},
            {'buffer': 0, 'byteOffset': len(positions), 'byteLength': len(indices)},
        ],
        'accessors': [
            {'bufferView': 0, 'componentType': 5126, 'count': 3, 'type': 'VEC3'},
            {'bufferView': 1, 'componentType': 5123, 'count': 3, 'type': 'SCALAR'},
        ],
        'meshes': [{'primitives': [{'attributes': {'POSITION': 0}, 'indices': 1}]}],
        'nodes': [{'mesh': 0, 'translation': [1, 2, 3]}],
        'scenes': [{'nodes': [0]}],
        'scene': 0,
    }
    json_bytes = json.dumps(document, separators=(',', ':')).encode('utf-8')
    json_bytes += b' ' * ((-len(json_bytes)) % 4)
    total = 12 + 8 + len(json_bytes) + 8 + len(binary)
    return (
        struct.pack('<4sII', b'glTF', 2, total)
        + struct.pack('<II', len(json_bytes), 0x4E4F534A)
        + json_bytes
        + struct.pack('<II', len(binary), 0x004E4942)
        + binary
    )


def test_obj_import_preserves_original_asset_and_does_not_claim_acoustic_semantics() -> None:
    asset = (Path(__file__).parent / 'fixtures' / 'imperfect_room.obj').read_bytes()
    mesh = import_raw_visual_mesh(asset, source_name='imperfect_room.obj')

    assert mesh.provenance.asset_format == 'obj'
    assert mesh.provenance.importer_version == '1'
    assert mesh.provenance.acoustic_semantics == 'unassigned'
    assert mesh.provenance.solver_readiness == 'raw_visual_only'
    assert mesh.original_asset_bytes() == asset
    assert mesh.mesh_id.endswith(mesh.provenance.original_asset_sha256)


def test_glb_import_applies_active_scene_node_transform_and_preserves_bytes() -> None:
    asset = _minimal_glb()
    mesh = import_raw_visual_mesh(asset, source_name='triangle.glb')

    assert mesh.provenance.asset_format == 'glb'
    assert [(v.x, v.y, v.z) for v in mesh.vertices] == [
        (1.0, 2.0, 3.0),
        (2.0, 2.0, 3.0),
        (1.0, 3.0, 3.0),
    ]
    assert len(mesh.triangles) == 1
    assert mesh.original_asset_bytes() == asset


def test_representative_imperfect_fixture_reports_required_diagnostics() -> None:
    mesh = import_raw_visual_mesh(
        (Path(__file__).parent / 'fixtures' / 'imperfect_room.obj').read_bytes(),
        source_name='imperfect_room.obj',
    )
    result = diagnose_raw_visual_mesh(mesh)
    findings = {finding.code: finding for finding in result.findings}

    assert findings['open_boundary'].state == 'fail'
    assert findings['non_manifold_edge'].state == 'fail'
    assert findings['duplicate_face'].state == 'fail'
    assert findings['overlapping_face'].state == 'fail'
    assert findings['inverted_normal'].state == 'fail'
    assert findings['sliver_face'].state == 'fail'
    assert findings['tiny_feature'].state == 'fail'
    assert findings['watertightness'].state == 'fail'
    assert result.acoustic_volume_readiness == 'not_ready'
    assert result.solver_ready is False
    assert result.semantic_conversion_required is True


def test_clean_closed_geometry_still_requires_semantic_conversion() -> None:
    asset = b'''\
v 0 0 0
v 1 0 0
v 0 1 0
v 0 0 1
f 1 3 2
f 1 2 4
f 1 4 3
f 2 3 4
'''
    mesh = import_raw_visual_mesh(asset, source_name='closed.obj')
    result = diagnose_raw_visual_mesh(mesh)
    findings = {finding.code: finding for finding in result.findings}

    assert all(finding.state == 'pass' for finding in findings.values())
    assert result.acoustic_volume_readiness == 'geometry_checks_pass_but_semantic_conversion_required'
    assert result.solver_ready is False


def test_snapshot_and_diagnostics_round_trip_with_deterministic_identity() -> None:
    asset = (Path(__file__).parent / 'fixtures' / 'imperfect_room.obj').read_bytes()
    first = import_raw_visual_mesh(asset, source_name='imperfect_room.obj')
    reopened = deserialize_raw_visual_mesh(serialize_raw_visual_mesh(first))
    assert reopened == first
    assert reopened.semantic_hash() == first.semantic_hash()

    first_result = diagnose_raw_visual_mesh(first)
    reopened_result = deserialize_raw_mesh_diagnostics(
        serialize_raw_mesh_diagnostics(first_result)
    )
    repeated_result = diagnose_raw_visual_mesh(reopened)
    assert reopened_result == first_result
    assert repeated_result == first_result
    assert repeated_result.semantic_hash() == first_result.semantic_hash()


def test_diagnostic_identity_binds_profile_versioned_thresholds() -> None:
    asset = (Path(__file__).parent / 'fixtures' / 'imperfect_room.obj').read_bytes()
    mesh = import_raw_visual_mesh(asset, source_name='imperfect_room.obj')
    default = diagnose_raw_visual_mesh(mesh)
    changed = diagnose_raw_visual_mesh(
        mesh,
        profile=RawMeshDiagnosticProfile(tiny_feature_relative_size=1.0e-5),
    )
    assert changed.diagnostic_id != default.diagnostic_id
    assert changed.profile_semantic_hash != default.profile_semantic_hash


def _glb_container(document: bytes, binary: bytes = b'') -> bytes:
    """Wrap arbitrary JSON/BIN payloads in a structurally valid GLB."""
    json_bytes = document + b' ' * ((-len(document)) % 4)
    chunks = struct.pack('<II', len(json_bytes), 0x4E4F534A) + json_bytes
    if binary:
        binary += b'\x00' * ((-len(binary)) % 4)
        chunks += struct.pack('<II', len(binary), 0x004E4942) + binary
    total = 12 + len(chunks)
    return struct.pack('<4sII', b'glTF', 2, total) + chunks


@pytest.mark.parametrize(
    'document',
    [
        # present-but-mistyped members surfaced AttributeError/TypeError
        # before the importer's error contract was enforced (#REV28)
        b'[1]',
        b'{"asset":[]}',
        b'{"asset":{"version":"2.0"},"meshes":"x"}',
        b'{"asset":{"version":"2.0"},"meshes":[{"primitives":"x"}]}',
        b'{"asset":{"version":"2.0"},"meshes":[{"primitives":[5]}]}',
        b'{"asset":{"version":"2.0"},"meshes":[{"primitives":[{"attributes":[]}]}]}',
        b'{"asset":{"version":"2.0"},"accessors":["x"],'
        b'"meshes":[{"primitives":[{"attributes":{"POSITION":0}}]}]}',
        b'{"asset":{"version":"2.0"},"scenes":[{"nodes":[0]}],"scene":0,'
        b'"nodes":["x"]}',
        b'{"asset":{"version":"2.0"},"scenes":[{"nodes":[0]}],"scene":0,'
        b'"nodes":[{"matrix":5}]}',
        b'{"asset":{"version":"2.0"},"scenes":[{"nodes":[0]}],"scene":0,'
        b'"nodes":[{"children":5}]}',
        b'{"asset":{"version":"2.0"},'
        b'"meshes":[{"primitives":[{"attributes":{"POSITION":5}}]}]}',
    ],
    ids=[
        'document_is_list',
        'asset_is_list',
        'meshes_is_string',
        'primitives_is_string',
        'primitive_is_int',
        'attributes_is_list',
        'accessor_is_string',
        'node_is_string',
        'node_matrix_is_int',
        'node_children_is_int',
        'accessor_index_out_of_range',
    ],
)
def test_glb_mistyped_document_members_reject_with_import_error(
    document: bytes,
) -> None:
    binary = struct.pack('<9f', 0, 0, 0, 1, 0, 0, 0, 1, 0)
    with pytest.raises(RawMeshImportError):
        import_raw_visual_mesh(
            _glb_container(document, binary), source_name='hostile.glb'
        )


def test_glb_unbounded_numeric_member_rejects_with_import_error() -> None:
    document = (
        b'{"asset":{"version":"2.0"},"buffers":[{"byteLength":36}],'
        b'"bufferViews":[{"buffer":0,"byteOffset":0,"byteLength":36}],'
        b'"accessors":[{"componentType":5126,"count":1e999,"type":"VEC3",'
        b'"bufferView":0}],'
        b'"meshes":[{"primitives":[{"attributes":{"POSITION":0}}]}]}'
    )
    binary = struct.pack('<9f', 0, 0, 0, 1, 0, 0, 0, 1, 0)
    with pytest.raises(RawMeshImportError):
        import_raw_visual_mesh(
            _glb_container(document, binary), source_name='hostile.glb'
        )


def test_glb_deeply_nested_json_chunk_rejects_with_import_error() -> None:
    # json.loads reports a deeply nested document as RecursionError, which
    # escaped the importer's error contract before #REV28.
    document = b'[' * 3000 + b']' * 3000
    with pytest.raises(RawMeshImportError):
        import_raw_visual_mesh(
            _glb_container(document), source_name='hostile.glb'
        )
