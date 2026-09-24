"""Field / as-built evidence attachments (#507).

Installation photos, as-built notes and verification files must be bound to
*exact* revisions/entities — never to "the room" in the vague sense — and must
never silently promote an entity to a verified state. This module provides the
immutable record types; binary payloads live in the scene repository's
content-addressed blob store keyed by SHA-256.

Contract properties:

- an :class:`EvidenceTarget` always names an exact authority (a SceneRevision
  id, an entity inside a pinned revision, a SystemVariant id, or another
  referenced authority id) — free-floating evidence is rejected;
- a :class:`FieldEvidenceRecord` requires at least one exact target and either
  a content-addressed asset or human-authored text;
- evidence records are append-only and carry their own semantic hash;
- attaching evidence performs **no auto-promotion**: nothing here mutates an
  entity, marks a section verified, or upgrades an installation status — the
  record is evidence *about* the target, not a state change *on* it.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


FIELD_EVIDENCE_SCHEMA_VERSION = 1
FIELD_EVIDENCE_AUTHORITY_VERSION = 'field-evidence-1'

#: What the evidence physically is.
FieldEvidenceKind = Literal[
    'installation_photo',
    'as_built_note',
    'verification_file',
    'measurement_recording',
    'cable_routing',
    'connector_photo',
    'other',
]

#: Which exact authority an evidence target names.
EvidenceTargetKind = Literal[
    'scene_revision',
    'scene_entity',
    'system_variant',
    'installation_section',
    'other',
]

EvidenceMediaKind = Literal[
    'image',
    'document',
    'recording',
    'data',
    'other',
]


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: Any) -> str:
    return sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


class EvidenceTarget(BaseModel):
    """One exact binding from an evidence record to stored authority.

    ``scene_entity`` requires both the pinning ``revision_id`` and the
    ``entity_id`` inside it, so an attachment can never drift onto a newer
    revision's idea of "the same" entity.
    """

    model_config = ConfigDict(frozen=True)

    kind: EvidenceTargetKind
    revision_id: str | None = Field(default=None, min_length=1)
    entity_id: str | None = Field(default=None, min_length=1)
    ref_id: str | None = Field(default=None, min_length=1)
    ref_sha256: str | None = Field(
        default=None, min_length=8, max_length=64
    )

    @model_validator(mode='after')
    def valid_target(self) -> 'EvidenceTarget':
        if self.kind == 'scene_entity':
            if not self.revision_id or not self.entity_id:
                raise ValueError(
                    'scene_entity targets require revision_id and entity_id'
                )
        elif self.kind == 'scene_revision':
            if not self.revision_id:
                raise ValueError('scene_revision targets require revision_id')
        else:
            if not self.ref_id:
                raise ValueError(f'{self.kind} targets require ref_id')
        return self


class FieldEvidenceAsset(BaseModel):
    """Metadata for one content-addressed evidence blob.

    The bytes live in the scene repository blob store; this record only
    carries the identity needed to fetch and verify them.
    """

    model_config = ConfigDict(frozen=True)

    asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    media_kind: EvidenceMediaKind
    filename: str | None = Field(default=None, min_length=1)
    mime_type: str | None = Field(default=None, min_length=1)
    byte_length: int = Field(ge=0)
    source_uri: str | None = Field(default=None, min_length=1)
    note: str | None = None


class FieldEvidenceRecord(BaseModel):
    """Immutable binding of one evidence item to exact targets."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = FIELD_EVIDENCE_SCHEMA_VERSION
    authority_version: Literal['field-evidence-1'] = (
        FIELD_EVIDENCE_AUTHORITY_VERSION
    )
    evidence_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    kind: FieldEvidenceKind
    targets: tuple[EvidenceTarget, ...] = Field(min_length=1)
    asset: FieldEvidenceAsset | None = None
    text: str | None = None
    captured_at_utc: str | None = None
    observer: str | None = None
    provenance: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    evidence_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_evidence(self) -> 'FieldEvidenceRecord':
        if self.asset is None and not self.text:
            raise ValueError(
                'evidence requires a content-addressed asset or text'
            )
        if self.evidence_sha256 != _hash(self.semantic_payload()):
            raise ValueError('FieldEvidenceRecord hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'evidence_id': self.evidence_id,
            'document_id': self.document_id,
            'kind': self.kind,
            'targets': [t.model_dump(mode='json') for t in self.targets],
            'asset': (
                self.asset.model_dump(mode='json') if self.asset else None
            ),
            'text': self.text,
            'captured_at_utc': self.captured_at_utc,
            'observer': self.observer,
            'provenance': self.provenance,
            'created_at_utc': self.created_at_utc,
        }


def build_field_evidence(
    *,
    document_id: str,
    kind: FieldEvidenceKind,
    targets: tuple[EvidenceTarget, ...],
    provenance: str,
    created_at_utc: str,
    asset: FieldEvidenceAsset | None = None,
    text: str | None = None,
    captured_at_utc: str | None = None,
    observer: str | None = None,
    evidence_id: str | None = None,
) -> FieldEvidenceRecord:
    payload: dict[str, Any] = {
        'evidence_id': evidence_id or str(uuid4()),
        'document_id': document_id,
        'kind': kind,
        'targets': tuple(targets),
        'asset': asset,
        'text': text,
        'captured_at_utc': captured_at_utc,
        'observer': observer,
        'provenance': provenance,
        'created_at_utc': created_at_utc,
    }
    provisional = FieldEvidenceRecord.model_construct(
        **payload, evidence_sha256='0' * 64
    )
    return FieldEvidenceRecord(
        **payload,
        evidence_sha256=_hash(provisional.semantic_payload()),
    )


__all__ = [
    'FIELD_EVIDENCE_AUTHORITY_VERSION',
    'FIELD_EVIDENCE_SCHEMA_VERSION',
    'EvidenceMediaKind',
    'EvidenceTarget',
    'EvidenceTargetKind',
    'FieldEvidenceAsset',
    'FieldEvidenceKind',
    'FieldEvidenceRecord',
    'build_field_evidence',
]
