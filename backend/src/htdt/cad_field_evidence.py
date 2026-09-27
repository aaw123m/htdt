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

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash


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

#: The canonical InstallationOutput section vocabulary an
#: ``installation_section`` target may name — section names are a stable
#: typed contract, never a free-form string.
INSTALLATION_SECTION_IDS: tuple[str, ...] = (
    'projector_coordinates',
    'standards_profile',
    'calibration_plan',
    'treatment_plan',
)

EvidenceMediaKind = Literal[
    'image',
    'document',
    'recording',
    'data',
    'other',
]






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
        default=None, pattern=r'^[0-9a-f]{64}$'
    )

    @model_validator(mode='after')
    def valid_target(self) -> 'EvidenceTarget':
        if self.kind == 'scene_entity':
            if not self.revision_id or not self.entity_id:
                raise ValueError(
                    'scene_entity targets require revision_id and entity_id'
                )
            # The entity lives inside the pinned revision; the pin must
            # name the revision's exact content hash so the target cannot
            # silently follow a later revision.
            if self.ref_sha256 is None:
                raise ValueError(
                    'scene_entity targets require the pinned revision '
                    'content hash as ref_sha256'
                )
        elif self.kind == 'scene_revision':
            if not self.revision_id:
                raise ValueError('scene_revision targets require revision_id')
            if self.ref_sha256 is None:
                raise ValueError(
                    'scene_revision targets require the revision content '
                    'hash as ref_sha256'
                )
        elif self.kind == 'system_variant':
            if not self.ref_id or self.ref_sha256 is None:
                raise ValueError(
                    'system_variant targets require ref_id and the exact '
                    'variant_sha256 pin'
                )
        elif self.kind == 'installation_section':
            if self.ref_id not in INSTALLATION_SECTION_IDS:
                raise ValueError(
                    'installation_section targets must name a canonical '
                    'section id'
                )
            if self.ref_sha256 is not None:
                raise ValueError(
                    'installation_section is a typed vocabulary, not a '
                    'hash-bearing authority'
                )
        else:  # 'other'
            if not self.ref_id or self.ref_sha256 is None:
                raise ValueError(
                    'other targets require ref_id plus an exact sha256 pin '
                    'of the external authority'
                )
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
    'INSTALLATION_SECTION_IDS',
    'EvidenceMediaKind',
    'EvidenceTarget',
    'EvidenceTargetKind',
    'FieldEvidenceAsset',
    'FieldEvidenceKind',
    'FieldEvidenceRecord',
    'build_field_evidence',
]
