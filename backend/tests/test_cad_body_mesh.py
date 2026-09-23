"""Issue #653 regression tests: mesh body asset boundary.

Mesh bodies persist as compact ``BodyMeshReference`` records pointing at
content-addressed HTDTMSH1 blobs — never as inline vertex arrays inside
every immutable SceneRevision payload.
"""

from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from htdt.cad_body_mesh import (
    MeshAssetUnavailableError,
    decode_body_mesh_bin,
    encode_body_mesh_bin,
    make_body_mesh_reference,
    missing_mesh_blob_digests,
    referenced_mesh_blob_digests,
    resolve_document_mesh_bodies,
    upgrade_document_mesh_bodies,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    BodyMeshAsset,
    BodyMeshTriangle,
    BodyMeshVertex,
    EntityBodyGeometry,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    canonical_scene_json,
    scene_content_hash,
)
from htdt.raw_mesh import import_raw_visual_mesh


DOCUMENT_ID = 'mesh-body-fixture'

_SIMPLE_OBJ = b"""# unit tetra body
v 0.0 0.0 0.0
v 0.4 0.0 0.0
v 0.0 0.3 0.0
v 0.0 0.0 0.2
f 1 2 3
f 1 2 4
f 1 3 4
f 2 3 4
"""


def _mesh_asset() -> BodyMeshAsset:
    imported = import_raw_visual_mesh(_SIMPLE_OBJ, source_name='tetra.obj')
    return BodyMeshAsset(
        asset_sha256=imported.provenance.original_asset_sha256,
        source_name=imported.provenance.source_name,
        asset_format=imported.provenance.asset_format,
        original_size_bytes=imported.provenance.original_size_bytes,
        vertices=tuple(
            BodyMeshVertex(x_m=v.x, y_m=v.y, z_m=v.z) for v in imported.vertices
        ),
        triangles=tuple(
            BodyMeshTriangle(a=t.a, b=t.b, c=t.c) for t in imported.triangles
        ),
    )


def _scene(*entities: SceneEntity) -> SceneDocument:
    return SceneDocument(
        document_id=DOCUMENT_ID,
        room=RoomPrism(width_m=8.0, depth_m=6.0, height_m=3.0),
        entities=entities,
    )


def _meshed_furniture(entity_id: str = 'rack', **overrides) -> SceneEntity:
    mesh = _mesh_asset()
    if 'body' in overrides:
        body = overrides.pop('body')
    else:
        body = EntityBodyGeometry(kind='mesh_asset', mesh=mesh)
    return SceneEntity(
        entity_id=entity_id,
        kind='furniture',
        name=entity_id,
        position=Position3(x_m=6.5, y_m=2.0, z_m=0.4),
        size_m=Size3(x_m=1.0, y_m=1.0, z_m=0.8),
        body_geometry=body,
        **overrides,
    )


def test_meshbin_round_trip() -> None:
    mesh = _mesh_asset()
    blob = encode_body_mesh_bin(mesh.vertices, mesh.triangles)
    assert blob[:8] == b'HTDTMSH1'
    vertices, triangles = decode_body_mesh_bin(blob)
    assert len(vertices) == len(mesh.vertices)
    assert len(triangles) == len(mesh.triangles)
    for decoded, source in zip(vertices, mesh.vertices):
        assert decoded.x_m == pytest.approx(float(source.x_m))
        assert decoded.y_m == pytest.approx(float(source.y_m))
        assert decoded.z_m == pytest.approx(float(source.z_m))
    assert triangles == mesh.triangles


def test_make_reference_records_bounds_and_digest() -> None:
    mesh = _mesh_asset()
    reference, blob = make_body_mesh_reference(mesh)
    assert reference.geometry_asset_sha256 == sha256(blob).hexdigest()
    assert reference.source_asset_sha256 == mesh.asset_sha256
    assert reference.source_name == 'tetra.obj'
    assert reference.vertex_count == len(mesh.vertices)
    assert reference.triangle_count == len(mesh.triangles)
    minimum, maximum = reference.transformed_bounds()
    assert maximum == pytest.approx((0.4, 0.3, 0.2))


def test_canonical_payload_drops_inline_mesh_for_reference_form() -> None:
    entity = _meshed_furniture()
    document = _scene(entity)
    upgraded = upgrade_document_mesh_bodies(document, lambda p: sha256(p).hexdigest())
    canonical = canonical_scene_json(upgraded)
    payload = json.loads(canonical)
    body = payload['entities'][0]['body_geometry']
    assert 'mesh' not in body  # vertex arrays are no longer persisted inline
    assert body['mesh_reference']['geometry_format'] == 'htdt_meshbin_v1'
    assert body['mesh_reference']['vertex_count'] == 4
    # The hash binds the compact reference, not the inline geometry.
    assert scene_content_hash(upgraded) != scene_content_hash(document)


def test_repository_persists_mesh_bodies_by_reference(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = _scene(_meshed_furniture())
    saved = repository.save(document, parent_revision_id=None)
    # The stored payload carries a reference, never the vertex arrays.
    head = repository.current_head(DOCUMENT_ID)
    assert head is not None
    body = head.document.entity('rack').body_geometry
    assert body is not None and body.mesh_reference is not None
    assert 'mesh' not in json.loads(
        canonical_scene_json(head.document)
    )['entities'][0]['body_geometry']
    # ...yet the resolved document exposes the full in-memory mesh.
    assert body.mesh is not None
    assert len(body.mesh.vertices) == 4
    assert head.document == saved.revision.document
    # The blob digest the revision binds is present in the store.
    digest = body.mesh_reference.geometry_asset_sha256
    assert repository.read_blob(digest) is not None
    assert referenced_mesh_blob_digests(head.document) == frozenset({digest})
    assert missing_mesh_blob_digests(head.document, repository.read_blob) == ()


def test_resolve_fails_closed_on_missing_or_corrupt_blob(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = _scene(_meshed_furniture())
    saved = repository.save(document, parent_revision_id=None).revision.document
    reference = saved.entity('rack').body_geometry.mesh_reference
    digest = reference.geometry_asset_sha256
    # Reference-only document (mesh cache dropped) resolves via the blob.
    stripped = SceneDocument.model_validate(
        json.loads(canonical_scene_json(saved))
    )
    body = stripped.entity('rack').body_geometry
    assert body.mesh is None and body.mesh_reference is not None
    assert missing_mesh_blob_digests(stripped, repository.read_blob) == ()
    resolved = resolve_document_mesh_bodies(stripped, repository.read_blob)
    assert resolved.entity('rack').body_geometry.mesh is not None
    # Missing blob → fail closed.
    def no_blob(_sha: str):
        return None
    assert missing_mesh_blob_digests(stripped, no_blob) == (digest,)
    with pytest.raises(MeshAssetUnavailableError, match='missing'):
        resolve_document_mesh_bodies(stripped, no_blob)
    # Corrupt blob (digest mismatch) → fail closed.
    with pytest.raises(MeshAssetUnavailableError, match='digest'):
        resolve_document_mesh_bodies(stripped, lambda _sha: b'garbage')


def test_upgrade_is_idempotent_for_referenced_bodies(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = _scene(_meshed_furniture())
    upgraded = upgrade_document_mesh_bodies(document, lambda p: sha256(p).hexdigest())
    again = upgrade_document_mesh_bodies(upgraded, lambda p: sha256(p).hexdigest())
    assert again is upgraded
