from hashlib import sha256
import struct

import pytest

from htdt.capture_mesh_ingestion import (
    CaptureMeshIngestionError,
    CaptureRawVisualMeshBinding,
    adapt_capture_mesh_handoff,
)


def _meshbin() -> bytes:
    vertices = struct.pack(
        '<9f',
        0.0, 0.0, 0.0,
        1.0, 0.0, 0.0,
        0.0, 1.0, 0.0,
    )
    normals = struct.pack(
        '<9f',
        0.0, 0.0, 1.0,
        0.0, 0.0, 1.0,
        0.0, 0.0, 1.0,
    )
    indices = struct.pack('<3I', 0, 1, 2)
    classifications = bytes([2])
    header = (
        b'HTDTMSH1'
        + struct.pack('<HH', 1, 0)
        + struct.pack('<I', 32)
        + struct.pack('<I', 3)
        + struct.pack('<I', 1)
        + bytes([4, 0x03])
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
