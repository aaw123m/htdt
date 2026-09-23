from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from htdt.raw_mesh import RawVisualMesh, import_raw_visual_mesh


CAPTURE_MESH_BINDING_DOMAIN = 'htdt.capture.raw-visual-mesh-binding.v1'
CAPTURE_MESH_BINDING_RECORD_SCHEMA = 'htdt.capture.raw-visual-mesh-binding-record'
CAPTURE_MESH_BINDING_RECORD_VERSION = '2.0.0'


class CaptureMeshIngestionError(ValueError):
    pass


class CaptureMatrix4x4F(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    representation: Literal['column_major_4x4_f32']
    values: tuple[float, ...]

    @field_validator('values')
    @classmethod
    def validate_values(cls, values: tuple[float, ...]) -> tuple[float, ...]:
        if len(values) != 16:
            raise ValueError('capture mesh transform must contain 16 values')
        if any(not isfinite(value) for value in values):
            raise ValueError('capture mesh transform values must be finite')
        return values


class CaptureMeshHandoff(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra='forbid')

    raw_visual_mesh_handoff_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    bundle_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    anchor_id: str = Field(
        pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    )
    anchor_record_locator: str = Field(min_length=1)
    anchor_index_source_evidence_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    geometry_source_evidence_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    geometry_path: str = Field(min_length=1)
    geometry_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    capture_session_id: str = Field(
        pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    )
    coordinate_space_id: str = Field(
        pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    )
    world_from_mesh_anchor: CaptureMatrix4x4F = Field(
        alias='T_world_from_mesh_anchor'
    )
    session_timestamp_s: float = Field(ge=0.0)
    vertex_count: int = Field(ge=0)
    face_count: int = Field(ge=0)

    @field_validator('session_timestamp_s')
    @classmethod
    def finite_timestamp(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError('capture mesh timestamp must be finite')
        return value


class CaptureRawVisualMeshBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    binding_id: str = Field(
        pattern=r'^capture-raw-mesh-binding:[0-9a-f]{64}$'
    )
    source_authority: Literal[
        'validated_htdt_capture_ingestion_plan_v1'
    ] = 'validated_htdt_capture_ingestion_plan_v1'
    handoff: CaptureMeshHandoff
    raw_mesh: RawVisualMesh
    solver_ready: Literal[False] = False

    @model_validator(mode='after')
    def validate_binding(self) -> 'CaptureRawVisualMeshBinding':
        if self.raw_mesh.provenance.asset_format != 'htdt_meshbin_v1':
            raise ValueError('capture binding requires HTDTMSH1 source bytes')
        if (
            self.raw_mesh.provenance.original_asset_sha256
            != self.handoff.geometry_sha256
        ):
            raise ValueError('capture binding geometry hash mismatch')
        if len(self.raw_mesh.vertices) != self.handoff.vertex_count:
            raise ValueError('capture binding vertex count mismatch')
        if len(self.raw_mesh.triangles) != self.handoff.face_count:
            raise ValueError('capture binding face count mismatch')
        expected = _binding_id(
            self.handoff.raw_visual_mesh_handoff_id,
            self.raw_mesh.semantic_hash(),
        )
        if self.binding_id != expected:
            raise ValueError('capture binding identity mismatch')
        return self

    def semantic_hash(self) -> str:
        return sha256(
            _canonical_json(self.model_dump(mode='json')).encode('utf-8')
        ).hexdigest()


def adapt_capture_mesh_handoff(
    handoff: CaptureMeshHandoff | Mapping[str, Any],
    geometry_bytes: bytes,
) -> CaptureRawVisualMeshBinding:
    if not isinstance(geometry_bytes, bytes):
        raise TypeError('geometry_bytes must be immutable bytes')

    typed = (
        handoff
        if isinstance(handoff, CaptureMeshHandoff)
        else CaptureMeshHandoff.model_validate(handoff)
    )

    actual_sha256 = sha256(geometry_bytes).hexdigest()
    if actual_sha256 != typed.geometry_sha256:
        raise CaptureMeshIngestionError(
            'capture geometry bytes do not match handoff SHA-256'
        )

    mesh = import_raw_visual_mesh(
        geometry_bytes,
        source_name=typed.geometry_path,
        format_hint='htdt_meshbin_v1',
    )

    if len(mesh.vertices) != typed.vertex_count:
        raise CaptureMeshIngestionError(
            'capture geometry vertex count does not match handoff'
        )
    if len(mesh.triangles) != typed.face_count:
        raise CaptureMeshIngestionError(
            'capture geometry face count does not match handoff'
        )

    return CaptureRawVisualMeshBinding(
        binding_id=_binding_id(
            typed.raw_visual_mesh_handoff_id,
            mesh.semantic_hash(),
        ),
        handoff=typed,
        raw_mesh=mesh,
    )


def serialize_mesh_binding_record(binding: CaptureRawVisualMeshBinding) -> str:
    """Compact persisted form of a capture mesh binding.

    The record keeps the binding identity and the validated handoff. The
    decoded mesh is rebuilt on read from the canonical content-addressed
    source bytes (keyed by ``handoff.geometry_sha256``) instead of embedding
    the raw asset again as Base64 plus expanded vertex/face arrays.
    """

    return _canonical_json(
        {
            'schema': CAPTURE_MESH_BINDING_RECORD_SCHEMA,
            'schema_version': CAPTURE_MESH_BINDING_RECORD_VERSION,
            'binding_id': binding.binding_id,
            'handoff': binding.handoff.model_dump(mode='json', by_alias=True),
        }
    )


def parse_mesh_binding_record(
    payload: str,
) -> tuple[str, CaptureMeshHandoff] | None:
    """Decode a compact persisted binding record.

    Returns ``(binding_id, handoff)`` for the compact representation, or None
    when the payload is a legacy fully-serialized CaptureRawVisualMeshBinding
    (which embeds ``raw_mesh`` and must be validated directly).
    """

    data = json.loads(payload)
    if not isinstance(data, dict) or 'raw_mesh' in data:
        return None
    if data.get('schema') != CAPTURE_MESH_BINDING_RECORD_SCHEMA:
        raise CaptureMeshIngestionError(
            'capture mesh binding record has an unknown schema'
        )
    if data.get('schema_version') != CAPTURE_MESH_BINDING_RECORD_VERSION:
        raise CaptureMeshIngestionError(
            'capture mesh binding record has an unsupported version'
        )
    binding_id = data.get('binding_id')
    if not isinstance(binding_id, str):
        raise CaptureMeshIngestionError(
            'capture mesh binding record is missing binding_id'
        )
    return binding_id, CaptureMeshHandoff.model_validate(data['handoff'])


def _binding_id(handoff_id: str, raw_mesh_semantic_hash: str) -> str:
    digest = sha256(CAPTURE_MESH_BINDING_DOMAIN.encode('utf-8'))
    digest.update(b'\x00')
    digest.update(handoff_id.encode('utf-8'))
    digest.update(b'\x00')
    digest.update(raw_mesh_semantic_hash.encode('utf-8'))
    return f'capture-raw-mesh-binding:{digest.hexdigest()}'


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )
