"""External 3D geometry import authority (Issue #525).

OBJ/PLY/STL/GLB assets are ingested as *provenance-preserving reference
meshes* — immutable captures of the source geometry — before any semantic
promotion. A reference mesh is NOT a scene entity, NOT acoustic solver
geometry, and NOT semantic room geometry: it is an auditable intermediate
that a later explicit promotion step can turn into an entity body
(:class:`BodyMeshAsset`) or R120 semantic geometry input.

Design contract:

- ``import_external_reference_mesh`` parses the source bytes (format-detected
  OBJ/PLY/STL/GLB), keeps the geometry in *source coordinates* (units are
  interpreted only when declared — an ``undeclared`` unit authority is a
  valid, honest reference state), encodes the mesh once as an
  ``HTDTMSH1`` content-addressed blob, and returns the compact
  ``ExternalReferenceMesh`` record. The original bytes' SHA-256 and the
  declared import authority are preserved verbatim.
- ``promote_reference_mesh_to_body`` is the only reference→Scene body
  boundary. It requires a resolvable declared unit (an ``undeclared``
  reference cannot be promoted until its units are operator-confirmed) and
  normalizes coordinates to entity-local meters under the recorded
  authority — the result is identical to a direct authoritative import of
  the same declarations, so content dedup is coherent.
- Reference meshes persist independent of Scene revisions: the record
  serializes canonically (``serialize_external_reference_mesh``) for
  project-side storage, and ``reference_mesh_blob_digests`` exports the
  blob dependencies for backup/GC.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from .cad_body_mesh import BODY_MESH_BLOB_FORMAT, decode_body_mesh_bin, encode_body_mesh_bin
from .cad_scene import (
    BodyMeshAsset,
    BodyMeshTriangle,
    BodyMeshVertex,
    MeshImportAuthority,
    Position3,
)
from .mesh_import_authority import (
    MESH_IMPORT_AUTHORITY_VERSION,
    make_mesh_import_authority,
    normalize_source_vertices,
)
from .raw_mesh import (
    RAW_MESH_IMPORTER_ID,
    RawMeshFormat,
    import_raw_visual_mesh,
)
from .canonical_json import canonical_json


EXTERNAL_REFERENCE_STATE = 'reference_only'


class MeshPromotionError(ValueError):
    """A reference mesh cannot be promoted under its recorded authority."""


class ExternalReferenceMesh(BaseModel):
    """A provenance-preserving reference mesh — pre-promotion geometry.

    Content-derived identity: ``reference_id`` binds the normalized blob
    digest plus the original asset digest, so re-importing the same asset
    produces the same reference. ``semantic_state`` is pinned to
    ``reference_only`` — this record can never be mistaken for solver or
    semantic geometry authority.
    """

    model_config = ConfigDict(frozen=True)

    reference_id: str = Field(min_length=1)
    source_name: str = Field(min_length=1)
    source_format: RawMeshFormat
    original_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    original_size_bytes: int = Field(ge=0)
    geometry_blob_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    geometry_format: Literal['htdt_meshbin_v1'] = BODY_MESH_BLOB_FORMAT
    vertex_count: int = Field(ge=1)
    triangle_count: int = Field(ge=1)
    bounds_min: Position3
    bounds_max: Position3
    import_authority: MeshImportAuthority
    semantic_state: Literal['reference_only'] = 'reference_only'


def _reference_id(geometry_blob_sha256: str, original_asset_sha256: str) -> str:
    digest = sha256(
        f'external-geometry:{geometry_blob_sha256}:{original_asset_sha256}'.encode('utf-8')
    ).hexdigest()
    return f'external-geometry:{digest}'


def _bounds(vertices) -> tuple[Position3, Position3]:
    xs = [v.x_m for v in vertices]
    ys = [v.y_m for v in vertices]
    zs = [v.z_m for v in vertices]
    return (
        Position3(x_m=min(xs), y_m=min(ys), z_m=min(zs)),
        Position3(x_m=max(xs), y_m=max(ys), z_m=max(zs)),
    )


def import_external_reference_mesh(
    asset: bytes,
    *,
    source_name: str,
    store_blob: Callable[[bytes], str],
    source_unit: str | None = None,
    unit_declared_by: str | None = None,
    custom_scale_to_meters: float | None = None,
    up_axis: str = 'unknown',
    forward_axis: str = 'unknown',
    handedness: str = 'unknown',
    format_hint: RawMeshFormat | None = None,
) -> ExternalReferenceMesh:
    """Ingest external geometry as a reference mesh and store its blob.

    Units may be ``undeclared`` at reference stage — the record stays
    faithful to exactly what was declared. ``store_blob`` persists the
    normalized-geometry HTDTMSH1 payload (e.g.
    ``SceneRepository.store_blob``) and returns its SHA-256 digest.
    """

    imported = import_raw_visual_mesh(asset, source_name=source_name, format_hint=format_hint)
    if imported.provenance.asset_format not in ('obj', 'ply', 'stl', 'glb', 'htdt_meshbin_v1'):
        raise MeshPromotionError(
            f'unsupported reference mesh format: {imported.provenance.asset_format}'
        )
    resolved_unit = source_unit
    resolved_by = unit_declared_by
    if resolved_unit is None:
        spec = 'meters' if imported.provenance.asset_format in ('glb', 'htdt_meshbin_v1') else None
        resolved_unit = spec if spec is not None else 'unknown'
        if resolved_by is None:
            resolved_by = 'format_specification' if spec is not None else 'undeclared'
    authority = make_mesh_import_authority(
        source_unit=resolved_unit,
        unit_declared_by=resolved_by or 'undeclared',
        custom_scale_to_meters=custom_scale_to_meters,
        up_axis=up_axis,
        forward_axis=forward_axis,
        handedness=handedness,
        local_anchor='source_origin',  # references keep raw source coordinates
        importer_id=RAW_MESH_IMPORTER_ID,
        importer_version=MESH_IMPORT_AUTHORITY_VERSION,
    )
    vertices = tuple(
        BodyMeshVertex(x_m=v.x, y_m=v.y, z_m=v.z)
        for v in imported.vertices
    )
    blob = encode_body_mesh_bin(
        vertices,
        [
            BodyMeshTriangle(a=t.a, b=t.b, c=t.c)
            for t in imported.triangles
        ],
    )
    blob_sha = store_blob(blob)
    minimum, maximum = _bounds(vertices)
    reference = ExternalReferenceMesh(
        reference_id=_reference_id(blob_sha, imported.provenance.original_asset_sha256),
        source_name=imported.provenance.source_name,
        source_format=imported.provenance.asset_format,
        original_asset_sha256=imported.provenance.original_asset_sha256,
        original_size_bytes=imported.provenance.original_size_bytes,
        geometry_blob_sha256=blob_sha,
        vertex_count=len(vertices),
        triangle_count=len(imported.triangles),
        bounds_min=minimum,
        bounds_max=maximum,
        import_authority=authority,
    )
    return reference


def serialize_external_reference_mesh(reference: ExternalReferenceMesh) -> str:
    return canonical_json(reference.model_dump(mode='json'))


def deserialize_external_reference_mesh(payload: str) -> ExternalReferenceMesh:
    return ExternalReferenceMesh.model_validate(json.loads(payload))


def promote_reference_mesh_to_body(
    reference: ExternalReferenceMesh,
    read_blob: Callable[[str], 'bytes | None'],
) -> BodyMeshAsset:
    """Promote a reference mesh to an entity-local normalized body asset.

    Requires a declared, resolvable unit: an ``undeclared`` reference raises
    — unit confirmation happens first via ``reconcile_mesh_import_authority``
    on the promoted body, or by re-importing the reference with an explicit
    operator-declared unit.
    """

    authority = reference.import_authority
    if authority.unit_declared_by in ('undeclared', 'legacy_assumed_meter'):
        raise MeshPromotionError(
            'reference mesh cannot be promoted without a declared source unit; '
            'confirm units first (operator-confirmed authority)'
        )
    payload = read_blob(reference.geometry_blob_sha256)
    if payload is None:
        raise MeshPromotionError(
            f'reference mesh blob {reference.geometry_blob_sha256} is missing'
        )
    if sha256(payload).hexdigest() != reference.geometry_blob_sha256:
        raise MeshPromotionError(
            f'reference mesh blob {reference.geometry_blob_sha256} failed digest verification'
        )
    vertices, triangles = decode_body_mesh_bin(payload)
    if len(vertices) != reference.vertex_count or len(triangles) != reference.triangle_count:
        raise MeshPromotionError('reference mesh blob does not match recorded counts')
    normalized = normalize_source_vertices(
        [(v.x_m, v.y_m, v.z_m) for v in vertices],
        authority,
    )
    return BodyMeshAsset(
        asset_sha256=reference.original_asset_sha256,
        source_name=reference.source_name,
        asset_format=reference.source_format,
        original_size_bytes=reference.original_size_bytes,
        vertices=normalized,
        triangles=triangles,
        import_authority=authority.model_copy(
            update={'importer_version': MESH_IMPORT_AUTHORITY_VERSION}
        ),
    )


def reference_mesh_blob_digests(reference: ExternalReferenceMesh) -> frozenset[str]:
    """Blob digests a reference mesh depends on (for backup/GC closure)."""

    return frozenset({reference.geometry_blob_sha256})
