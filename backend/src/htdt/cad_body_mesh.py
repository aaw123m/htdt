"""Mesh body asset boundary (Issue #653).

Every new SceneRevision previously embedded the full vertex/triangle arrays of
each ``mesh_asset`` body inline in the canonical Scene payload. That made
revision payloads large and repeated the same geometry in every revision of a
document.

This module moves mesh bodies to a compact reference form:

- The entity-local vertex/triangle arrays are encoded once as an
  ``HTDTMSH1`` meshbin payload and stored in the project content-addressed
  blob store (``SceneRepository.store_blob``).
- The Scene payload carries a :class:`BodyMeshReference` — the blob digest,
  provenance fields, the local transform and the transformed bounds — so
  revision identity binds exact mesh content by digest and broad-phase checks
  never require decoding the blob.
- On load, the repository resolves the reference through the blob store and
  rehydrates the in-memory ``mesh`` cache, so render/collision consumers keep
  working on resolved geometry. A missing or corrupt blob fails closed.

``upgrade_document_mesh_bodies`` is the single upgrade authority applied on
every SceneRevision/save path; ``resolve_document_mesh_bodies`` is the single
load-time resolver.
"""

from __future__ import annotations

from hashlib import sha256
import struct
from typing import Callable, Sequence

from .cad_scene import (
    BodyMeshAsset,
    BodyMeshReference,
    BodyMeshTriangle,
    BodyMeshVertex,
    Position3,
    SceneDocument,
)
from .raw_mesh import RawMeshImportError, _parse_htdt_meshbin_v1


BODY_MESH_BLOB_FORMAT = 'htdt_meshbin_v1'

_BODY_MESH_MAGIC = b'HTDTMSH1'
_BODY_MESH_HEADER_LEN = 32


class MeshAssetUnavailableError(ValueError):
    """A persisted BodyMeshReference could not be resolved from the blob store."""


def encode_body_mesh_bin(
    vertices: Sequence[BodyMeshVertex],
    triangles: Sequence[BodyMeshTriangle],
) -> bytes:
    """Encode entity-local mesh arrays as an ``HTDTMSH1`` v1.0 payload.

    Layout: 32-byte header (magic, version 1.0, header_len=32, vertex_count,
    face_count, index_width=4, flags=0, reserved=0), then f32 vertex triples,
    then u32 index triples — the exact format ``_parse_htdt_meshbin_v1``
    reads. No normals or face classifications are stored: those are source
    provenance kept on the raw-mesh record, not normalized body geometry.
    """

    if not vertices:
        raise ValueError('body mesh requires at least one vertex')
    if not triangles:
        raise ValueError('body mesh requires at least one triangle')
    header = bytearray(_BODY_MESH_HEADER_LEN)
    header[:8] = _BODY_MESH_MAGIC
    struct.pack_into('<HH', header, 8, 1, 0)
    struct.pack_into('<I', header, 12, _BODY_MESH_HEADER_LEN)
    struct.pack_into('<I', header, 16, len(vertices))
    struct.pack_into('<I', header, 20, len(triangles))
    header[24] = 4  # UInt32 index width
    header[25] = 0  # no normals, no classifications
    payload = bytearray(header)
    for vertex in vertices:
        payload += struct.pack(
            '<fff',
            float(vertex.x_m),
            float(vertex.y_m),
            float(vertex.z_m),
        )
    for triangle in triangles:
        payload += struct.pack('<III', triangle.a, triangle.b, triangle.c)
    return bytes(payload)


def decode_body_mesh_bin(
    payload: bytes,
) -> tuple[tuple[BodyMeshVertex, ...], tuple[BodyMeshTriangle, ...]]:
    """Decode an ``HTDTMSH1`` v1.0 blob into body mesh arrays."""

    try:
        vertices, triangles, _normals, _classifications = _parse_htdt_meshbin_v1(payload)
    except RawMeshImportError as error:
        raise MeshAssetUnavailableError(str(error)) from error
    return (
        tuple(
            BodyMeshVertex(x_m=vertex.x, y_m=vertex.y, z_m=vertex.z)
            for vertex in vertices
        ),
        tuple(
            BodyMeshTriangle(a=triangle.a, b=triangle.b, c=triangle.c)
            for triangle in triangles
        ),
    )


def make_body_mesh_reference(
    mesh: BodyMeshAsset,
) -> tuple[BodyMeshReference, bytes]:
    """Build the compact Scene reference + blob payload for an inline mesh body."""

    blob = encode_body_mesh_bin(mesh.vertices, mesh.triangles)
    minimum, maximum = mesh.transformed_bounds()
    reference = BodyMeshReference(
        geometry_asset_sha256=sha256(blob).hexdigest(),
        source_asset_sha256=mesh.asset_sha256,
        source_name=mesh.source_name,
        source_format=mesh.asset_format,
        original_size_bytes=mesh.original_size_bytes,
        local_offset_m=mesh.local_offset_m,
        uniform_scale=mesh.uniform_scale,
        bounds_min_m=Position3(x_m=minimum[0], y_m=minimum[1], z_m=minimum[2]),
        bounds_max_m=Position3(x_m=maximum[0], y_m=maximum[1], z_m=maximum[2]),
        vertex_count=len(mesh.vertices),
        triangle_count=len(mesh.triangles),
        import_authority=mesh.import_authority,
    )
    return reference, blob


def upgrade_document_mesh_bodies(
    document: SceneDocument,
    store_blob: Callable[[bytes], str],
) -> SceneDocument:
    """Convert inline ``mesh_asset`` bodies to their referenced form.

    ``store_blob`` persists the encoded HTDTMSH1 payload and returns its
    SHA-256 digest (used to verify the freshly built reference binds the
    stored content). Entities already in referenced form pass through. The
    resolved ``mesh`` cache is kept in memory; canonical serialization drops
    it, so every new SceneRevision payload carries only the compact
    reference.
    """

    changed = False
    entities = list(document.entities)
    for index, entity in enumerate(entities):
        body = entity.body_geometry
        if body is None or body.kind != 'mesh_asset':
            continue
        if body.mesh is None or body.mesh_reference is not None:
            continue
        reference, blob = make_body_mesh_reference(body.mesh)
        stored_digest = store_blob(blob)
        if stored_digest != reference.geometry_asset_sha256:
            raise MeshAssetUnavailableError(
                'mesh blob store returned an unexpected digest'
            )
        entities[index] = entity.model_copy(
            update={'body_geometry': body.model_copy(update={'mesh_reference': reference})}
        )
        changed = True
    if not changed:
        return document
    return document.model_copy(update={'entities': tuple(entities)})


def resolve_document_mesh_bodies(
    document: SceneDocument,
    read_blob: Callable[[str], 'bytes | None'],
) -> SceneDocument:
    """Rehydrate in-memory ``mesh`` caches for referenced mesh bodies.

    Fails closed: a missing blob, a digest mismatch, a malformed payload, or
    geometry that does not match the recorded counts/bounds raises
    ``MeshAssetUnavailableError`` rather than rendering an unverifiable body.
    """

    changed = False
    entities = list(document.entities)
    for index, entity in enumerate(entities):
        body = entity.body_geometry
        if body is None or body.kind != 'mesh_asset':
            continue
        reference = body.mesh_reference
        if reference is None or body.mesh is not None:
            continue
        payload = read_blob(reference.geometry_asset_sha256)
        if payload is None:
            raise MeshAssetUnavailableError(
                f'mesh body blob {reference.geometry_asset_sha256} is missing'
            )
        if sha256(payload).hexdigest() != reference.geometry_asset_sha256:
            raise MeshAssetUnavailableError(
                f'mesh body blob {reference.geometry_asset_sha256} failed digest verification'
            )
        vertices, triangles = decode_body_mesh_bin(payload)
        if len(vertices) != reference.vertex_count or len(triangles) != reference.triangle_count:
            raise MeshAssetUnavailableError(
                f'mesh body blob {reference.geometry_asset_sha256} does not match reference counts'
            )
        mesh = BodyMeshAsset(
            asset_sha256=reference.source_asset_sha256,
            source_name=reference.source_name,
            asset_format=reference.source_format,
            original_size_bytes=reference.original_size_bytes,
            local_offset_m=reference.local_offset_m,
            uniform_scale=reference.uniform_scale,
            vertices=vertices,
            triangles=triangles,
            import_authority=reference.import_authority,
        )
        entities[index] = entity.model_copy(
            update={'body_geometry': body.model_copy(update={'mesh': mesh})}
        )
        changed = True
    if not changed:
        return document
    return document.model_copy(update={'entities': tuple(entities)})


def referenced_mesh_blob_digests(document: SceneDocument) -> frozenset[str]:
    """All content-blob digests a document's mesh bodies depend on.

    Backup/export tools and garbage collection must carry or retain exactly
    this set for the document's mesh bodies to stay resolvable.
    """

    digests: set[str] = set()
    for entity in document.entities:
        body = entity.body_geometry
        if body is None or body.kind != 'mesh_asset':
            continue
        if body.mesh_reference is not None:
            digests.add(body.mesh_reference.geometry_asset_sha256)
    return frozenset(digests)


def missing_mesh_blob_digests(
    document: SceneDocument,
    read_blob: Callable[[str], 'bytes | None'],
) -> tuple[str, ...]:
    """Referenced mesh blob digests that cannot currently be resolved."""

    missing: list[str] = []
    for digest in sorted(referenced_mesh_blob_digests(document)):
        if read_blob(digest) is None:
            missing.append(digest)
    return tuple(missing)
