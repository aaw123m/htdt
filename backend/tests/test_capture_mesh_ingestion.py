from hashlib import sha256
import struct

import pytest

from htdt.capture_mesh_ingestion import (
    CaptureMeshIngestionError,
    CaptureRawVisualMeshBinding,
    adapt_capture_mesh_handoff,
)
from htdt.raw_mesh import (
    RawVisualMesh,
    import_raw_visual_mesh,
    meshbin_face_classification_label,
)


def _meshbin(
    *,
    with_normals: bool = True,
    with_classifications: bool = True,
    classification_bytes: bytes = bytes([2]),
) -> bytes:
    vertices = struct.pack(
        '<9f',
        0.0, 0.0, 0.0,
        1.0, 0.0, 0.0,
        0.0, 1.0, 0.0,
    )
    normals = (
        struct.pack(
            '<9f',
            0.0, 0.0, 1.0,
            0.0, 0.0, 1.0,
            0.0, 0.0, 1.0,
        )
        if with_normals
        else b''
    )
    indices = struct.pack('<3I', 0, 1, 2)
    classifications = (
        classification_bytes if with_classifications else b''
    )
    flags = (0x01 if with_normals else 0) | (
        0x02 if with_classifications else 0
    )
    header = (
        b'HTDTMSH1'
        + struct.pack('<HH', 1, 0)
        + struct.pack('<I', 32)
        + struct.pack('<I', 3)
        + struct.pack('<I', 1)
        + bytes([4, flags])
        + struct.pack('<H', 0)
        + struct.pack('<I', 0)
    )
    return header + vertices + normals + indices + classifications


def _handoff(asset: bytes, **overrides) -> dict:
    values = {
        'raw_visual_mesh_handoff_id': '1' * 64,
        'bundle_digest': '2' * 64,
        'anchor_id': '10000000-0000-4000-8000-000000000005',
        'anchor_record_locator': (
            'mesh/anchors.json#anchor:'
            '10000000-0000-4000-8000-000000000005'
        ),
        'anchor_index_source_evidence_id': '3' * 64,
        'geometry_source_evidence_id': '4' * 64,
        'geometry_path': (
            'mesh/geometry/'
            '10000000-0000-4000-8000-000000000005.meshbin'
        ),
        'geometry_sha256': sha256(asset).hexdigest(),
        'capture_session_id': '10000000-0000-4000-8000-000000000003',
        'coordinate_space_id': '10000000-0000-4000-8000-000000000004',
        'T_world_from_mesh_anchor': {
            'representation': 'column_major_4x4_f32',
            'values': [
                1, 0, 0, 0,
                0, 1, 0, 0,
                0, 0, 1, 0,
                2, 3, 4, 1,
            ],
        },
        'session_timestamp_s': 12.5,
        'vertex_count': 3,
        'face_count': 1,
    }
    values.update(overrides)
    return values


def test_capture_meshbin_maps_to_raw_visual_mesh_without_losing_source_bytes() -> None:
    asset = _meshbin()
    binding = adapt_capture_mesh_handoff(_handoff(asset), asset)

    assert binding.raw_mesh.provenance.asset_format == 'htdt_meshbin_v1'
    assert binding.raw_mesh.provenance.importer_version == '2'
    assert binding.raw_mesh.original_asset_bytes() == asset
    assert binding.raw_mesh.provenance.original_asset_sha256 == sha256(asset).hexdigest()
    assert [(v.x, v.y, v.z) for v in binding.raw_mesh.vertices] == [
        (0.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
    ]
    assert len(binding.raw_mesh.triangles) == 1
    assert binding.handoff.coordinate_space_id == (
        '10000000-0000-4000-8000-000000000004'
    )
    assert binding.handoff.world_from_mesh_anchor.values[12:15] == (2.0, 3.0, 4.0)
    assert binding.solver_ready is False


def test_capture_binding_round_trip_preserves_exact_lineage() -> None:
    asset = _meshbin()
    binding = adapt_capture_mesh_handoff(_handoff(asset), asset)

    reopened = CaptureRawVisualMeshBinding.model_validate_json(
        binding.model_dump_json()
    )

    assert reopened == binding
    assert reopened.semantic_hash() == binding.semantic_hash()


def test_capture_adapter_rejects_geometry_hash_mismatch_before_import() -> None:
    asset = _meshbin()
    handoff = _handoff(asset, geometry_sha256='f' * 64)

    with pytest.raises(
        CaptureMeshIngestionError,
        match='do not match handoff SHA-256',
    ):
        adapt_capture_mesh_handoff(handoff, asset)


def test_capture_adapter_rejects_count_mismatch() -> None:
    asset = _meshbin()
    handoff = _handoff(asset, face_count=2)

    with pytest.raises(
        CaptureMeshIngestionError,
        match='face count does not match handoff',
    ):
        adapt_capture_mesh_handoff(handoff, asset)


def test_capture_meshbin_retains_source_normals_and_face_classifications(
) -> None:
    asset = _meshbin()
    binding = adapt_capture_mesh_handoff(_handoff(asset), asset)
    mesh = binding.raw_mesh

    assert [(n.x, n.y, n.z) for n in mesh.source_normals] == [
        (0.0, 0.0, 1.0),
        (0.0, 0.0, 1.0),
        (0.0, 0.0, 1.0),
    ]
    assert mesh.source_face_classifications == (2,)
    assert meshbin_face_classification_label(
        mesh.source_face_classifications[0]
    ) == 'floor'


def test_capture_meshbin_without_optional_attributes_stays_valid() -> None:
    asset = _meshbin(with_normals=False, with_classifications=False)
    binding = adapt_capture_mesh_handoff(_handoff(asset), asset)

    assert binding.raw_mesh.source_normals == ()
    assert binding.raw_mesh.source_face_classifications == ()


def test_capture_meshbin_unknown_classification_byte_is_retained() -> None:
    asset = _meshbin(classification_bytes=bytes([42]))
    binding = adapt_capture_mesh_handoff(_handoff(asset), asset)

    assert binding.raw_mesh.source_face_classifications == (42,)
    assert meshbin_face_classification_label(42) == 'unknown:42'


def test_source_attributes_are_advisory_and_do_not_change_mesh_identity(
) -> None:
    """Normals/classifications are derived from the pinned asset bytes, so
    they must not perturb the semantic hash a persisted binding recorded."""
    asset = _meshbin()
    mesh = adapt_capture_mesh_handoff(_handoff(asset), asset).raw_mesh
    again = adapt_capture_mesh_handoff(_handoff(asset), asset).raw_mesh

    assert mesh.semantic_hash() == again.semantic_hash()
    assert 'source_normals' not in mesh.model_dump(
        mode='json',
        exclude={'source_normals', 'source_face_classifications'},
    )


def test_raw_visual_mesh_round_trip_preserves_source_attributes() -> None:
    asset = _meshbin()
    mesh = adapt_capture_mesh_handoff(_handoff(asset), asset).raw_mesh

    reopened = RawVisualMesh.model_validate_json(mesh.model_dump_json())

    assert reopened == mesh
    assert reopened.source_normals == mesh.source_normals
    assert reopened.source_face_classifications == (
        mesh.source_face_classifications
    )


def test_raw_visual_mesh_rejects_misaligned_source_attributes() -> None:
    asset = _meshbin()
    mesh = adapt_capture_mesh_handoff(_handoff(asset), asset).raw_mesh
    payload = mesh.model_dump(mode='json')
    payload['source_normals'] = payload['source_normals'][:1]

    with pytest.raises(ValueError, match='align one-to-one with vertices'):
        RawVisualMesh.model_validate(payload)

    payload = mesh.model_dump(mode='json')
    payload['source_face_classifications'] = [1, 2]
    with pytest.raises(ValueError, match='align one-to-one with triangles'):
        RawVisualMesh.model_validate(payload)


def test_source_attributes_require_meshbin_provenance() -> None:
    obj = b'v 0 0 0' + b'\nv 1 0 0\nv 0 1 0\nf 1 2 3\n'
    mesh = import_raw_visual_mesh(obj, source_name='a.obj')
    assert mesh.source_normals == ()

    payload = mesh.model_dump(mode='json')
    payload['source_normals'] = [{'x': 0.0, 'y': 0.0, 'z': 1.0}] * 3
    with pytest.raises(ValueError, match='require HTDTMSH1 provenance'):
        RawVisualMesh.model_validate(payload)
