"""Shared builders for contract-valid Capture Bundle v1 fixtures.

The ingestion boundary validates the pinned Capture contract against the
exact payload bytes, so every test plan must come from a bundle that
passes the shared schema/binary/metadata pipeline. These helpers compose
bundles from the vendored phase6 fixture (or caller-specified file sets),
emit canonical manifests, and return the reference ingestion plan plus
the exact payload mapping the transaction consumes.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import shutil
import struct

from htdt.capture_bundle import (
    FrozenBundle,
    canonical_json_bytes,
    canonical_payload_json_bytes,
)
from htdt.capture_reference import build_ingestion_plan


FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / 'fixtures'
    / 'capture'
    / 'phase6-integration'
)

FIXTURE_MANIFEST_BYTES = (FIXTURE_DIR / 'manifest.json').read_bytes()
FIXTURE_MANIFEST = json.loads(FIXTURE_MANIFEST_BYTES)

BUNDLE_DIGEST = sha256(FIXTURE_MANIFEST_BYTES).hexdigest()
SERIES_ID = FIXTURE_MANIFEST['capture_series_id']
REVISION_ID = FIXTURE_MANIFEST['capture_revision_id']
SESSION_ID = FIXTURE_MANIFEST['capture_session_ids'][0]
SPACE_ID = FIXTURE_MANIFEST['coordinate_space_ids'][0]
ANCHOR_ID = '10000000-0000-4000-8000-000000000005'
ANNOTATION_ID = '10000000-0000-4000-8000-000000000006'
MEASUREMENT_ID = '10000000-0000-4000-8000-000000000007'

DEFAULT_MEDIA_JSON = 'application/json'


def fixture_payloads() -> dict[str, bytes]:
    """Exact payload bytes of the vendored bundle (manifest excluded)."""
    return {
        entry['path']: (FIXTURE_DIR / entry['path']).read_bytes()
        for entry in FIXTURE_MANIFEST['files']
    }


def fixture_meta() -> dict[str, dict]:
    """Per-path manifest metadata of the vendored bundle."""
    return {
        entry['path']: {
            'media_type': entry['media_type'],
            'producer': entry['producer'],
            'provenance_class': entry['provenance_class'],
            'role': entry['role'],
            'source_refs': list(entry.get('source_refs', [])),
        }
        for entry in FIXTURE_MANIFEST['files']
    }


def meshbin(
    vertex_count: int = 3,
    face_count: int = 1,
    *,
    normals: bool = False,
    classifications: tuple[int, ...] | None = None,
) -> bytes:
    vertices = b''.join(
        struct.pack(
            '<3f',
            float(i % 4),
            float((i // 4) % 4),
            float(i % 2) * 0.5,
        )
        for i in range(vertex_count)
    )
    normal_bytes = (
        b''.join(
            struct.pack('<3f', 0.0, 0.0, 1.0)
            for _ in range(vertex_count)
        )
        if normals
        else b''
    )
    indices = b''.join(
        struct.pack('<3I', 0, 1, 2) for _ in range(face_count)
    )
    classification_bytes = (
        bytes(classifications) if classifications else b''
    )
    flags = (1 if normals else 0) | (2 if classifications else 0)
    header = (
        b'HTDTMSH1'
        + struct.pack('<HH', 1, 0)
        + struct.pack('<I', 32)
        + struct.pack('<I', vertex_count)
        + struct.pack('<I', face_count)
        + bytes([4, flags])
        + struct.pack('<H', 0)
        + struct.pack('<I', 0)
    )
    return (
        header + vertices + normal_bytes + indices
        + classification_bytes
    )


def write_bundle(
    dest: Path,
    files: dict[str, dict],
    *,
    manifest_overrides: dict | None = None,
) -> tuple[Path, bytes]:
    """Write a canonical bundle directory.

    ``files`` maps logical paths to {'bytes': bytes, 'media_type': str,
    'producer': str, 'provenance_class': str, 'role': str,
    'source_refs': [...]}. The manifest is emitted in Capture Bundle v1
    canonical form so ``sha256(manifest) == bundle_digest``; identity
    fields default to the vendored fixture's and may be overridden via
    ``manifest_overrides`` (also supporting 'app'/'created_at'/
    'finalized_at' replacements).
    """
    entries = []
    for path in sorted(files, key=lambda p: p.encode('utf-8')):
        spec = files[path]
        entry = {
            'path': path,
            'bytes': len(spec['bytes']),
            'media_type': spec['media_type'],
            'sha256': sha256(spec['bytes']).hexdigest(),
            'producer': spec['producer'],
            'provenance_class': spec['provenance_class'],
            'role': spec['role'],
        }
        if spec.get('source_refs'):
            entry['source_refs'] = list(spec['source_refs'])
        entries.append(entry)

    manifest = {
        'app': dict(FIXTURE_MANIFEST['app']),
        'capture_revision_id': REVISION_ID,
        'capture_series_id': SERIES_ID,
        'capture_session_ids': [SESSION_ID],
        'coordinate_space_ids': [SPACE_ID],
        'created_at': FIXTURE_MANIFEST['created_at'],
        'files': entries,
        'finalized_at': FIXTURE_MANIFEST['finalized_at'],
        'parent_revision_id': None,
        'schema': FIXTURE_MANIFEST['schema'],
        'schema_version': FIXTURE_MANIFEST['schema_version'],
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    manifest_bytes = canonical_json_bytes(manifest)

    for path, spec in files.items():
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(spec['bytes'])
    (dest / 'manifest.json').write_bytes(manifest_bytes)
    return dest, manifest_bytes


def default_file_specs() -> dict[str, dict]:
    """File specs replicating the vendored bundle (path → spec)."""
    payloads = fixture_payloads()
    meta = fixture_meta()
    return {
        path: {'bytes': payloads[path], **meta[path]}
        for path in payloads
    }


def plan_and_payloads(
    tmp_path: Path,
    *,
    files: dict[str, dict] | None = None,
    drop: tuple[str, ...] = (),
    manifest_overrides: dict | None = None,
    json_overrides: dict[str, object] | None = None,
    id_map: dict[str, str] | None = None,
) -> tuple[dict, dict[str, bytes], bytes]:
    """Materialize a bundle, then return (plan, payloads, manifest bytes).

    ``files`` replaces the default vendored file set entirely; ``drop``
    removes paths from it; ``json_overrides`` rewrites a JSON payload
    with a caller-supplied object (canonical-serialized) for tests that
    need mutated documents; ``manifest_overrides`` changes manifest
    identity fields (e.g. a second bundle digest for dedup tests).
    """
    specs = default_file_specs() if files is None else dict(files)
    for path in drop:
        specs.pop(path, None)
    for path, document in (json_overrides or {}).items():
        specs[path] = dict(specs[path], bytes=canonical_payload_json_bytes(document))
    if id_map:
        for path, spec in specs.items():
            if spec['media_type'] != 'application/json':
                continue
            text = spec['bytes'].decode('utf-8')
            for old, new in id_map.items():
                text = text.replace(old, new)
            specs[path] = dict(spec, bytes=text.encode('utf-8'))
    bundle_dir = tmp_path / 'bundle'
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True)
    _, manifest_bytes = write_bundle(
        bundle_dir, specs, manifest_overrides=manifest_overrides
    )

    frozen = FrozenBundle(bundle_dir)
    plan = build_ingestion_plan(frozen)
    payloads = {
        path: spec['bytes'] for path, spec in specs.items()
    }
    return plan, payloads, manifest_bytes


def mesh_specs_files(
    specs: tuple[tuple[str, str, int, int], ...],
    *,
    mesh_payloads: dict[str, bytes] | None = None,
    anchor_transform: tuple[float, ...] | None = None,
    session_id: str = SESSION_ID,
    space_id: str = SPACE_ID,
) -> dict[str, dict]:
    """File specs for a mesh-focused variant: foundation/session set plus
    one mesh anchor per (anchor_id, geometry_path, vertex_count,
    face_count) spec — used by dedup tests that vary geometry bytes."""
    files = {
        path: spec
        for path, spec in default_file_specs().items()
        if path.startswith('session/') or path.startswith('quality/')
    }
    geometry_by_path: dict[str, bytes] = {}
    for _, geometry_path, vertex_count, face_count in specs:
        payload = (
            (mesh_payloads or {}).get(geometry_path)
            or meshbin(vertex_count, face_count)
        )
        geometry_by_path[geometry_path] = payload
        files[geometry_path] = {
            'bytes': payload,
            'media_type': 'application/vnd.htdt.meshbin',
            'producer': 'mesh_capture',
            'provenance_class': 'arkit_mesh_reconstruction',
            'role': 'canonical',
        }
    anchors = {
        'schema': 'htdt.capture.mesh-anchors',
        'schema_version': '1.0.0',
        'anchors': [
            {
                'anchor_id': anchor_id,
                'capture_session_id': session_id,
                'coordinate_space_id': space_id,
                'T_world_from_mesh_anchor': {
                    'representation': 'column_major_4x4_f32',
                    'values': list(
                        anchor_transform
                        or (
                            1, 0, 0, 0,
                            0, 1, 0, 0,
                            0, 0, 1, 0,
                            0, 0, 0, 1,
                        )
                    ),
                },
                'geometry_path': geometry_path,
                'geometry_sha256': sha256(
                    geometry_by_path[geometry_path]
                ).hexdigest(),
                'session_timestamp_s': 1.0,
                'vertex_count': vertex_count,
                'face_count': face_count,
            }
            for anchor_id, geometry_path, vertex_count, face_count in specs
        ],
    }
    files['mesh/anchors.json'] = {
        'bytes': canonical_payload_json_bytes(anchors),
        'media_type': 'application/json',
        'producer': 'mesh_capture',
        'provenance_class': 'arkit_mesh_reconstruction',
        'role': 'canonical',
        'source_refs': [
            f'path:{geometry_path}'
            for _, geometry_path, _, _ in specs
        ],
    }
    return files
