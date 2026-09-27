"""Site / space hierarchy authority (#729).

One project can hold a theater plus adjacent/support spaces — each with
stable identity, role, explicit frame relationship to the site frame, and
typed provenance-preserving relationships. This is NOT general BIM:
spaces exist only where they matter to theater geometry, connected
acoustics, sound isolation, installation/routing, equipment
infrastructure, or field evidence.

A space's geometry is always a reference to an authority (capture run,
manual geometry, imported mesh) — or explicitly ``None`` for UNKNOWN
geometry; the hierarchy never invents connectivity from overlapping
meshes. Changing a space's site transform is an authority/reconciliation
decision, not a silent move of historical evidence.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_schema import ensure_native_schema, require_native_tables
from .r120_geometry_compiler import ExactExternalAuthorityRef
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash


SpaceKind = Literal['theater', 'adjacent', 'equipment', 'support']

SpaceRelationshipKind = Literal[
    'adjacent_boundary',
    'opening_portal',
    'cable_route',
    'equipment_service',
    'isolation_source_receiver',
]

_SPACE_PREFIX = 'space:'
_REL_PREFIX = 'space-rel:'


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()






def _require_iso8601(value: str, name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{name} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{name} must be timezone-aware')


class FrameTransform4x4(BaseModel):
    """Exact rigid/affine relationship between a space frame and the site
    frame, stored column-major like the capture manifest convention."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    column_major_4x4: tuple[float, ...]

    @model_validator(mode='after')
    def valid_transform(self) -> 'FrameTransform4x4':
        m = self.column_major_4x4
        if len(m) != 16:
            raise ValueError('frame transform must contain 16 values')
        if any(not isfinite(v) for v in m):
            raise ValueError('frame transform must be finite')
        last_row = (m[3], m[7], m[11], m[15])
        if last_row != (0.0, 0.0, 0.0, 1.0):
            raise ValueError(
                'frame transform must be affine — last row (0,0,0,1)'
            )
        return self


IDENTITY_TRANSFORM = FrameTransform4x4(
    column_major_4x4=(
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    )
)


class SiteSpace(BaseModel):
    """One space in the project site hierarchy (#729).

    ``geometry_ref`` binds the space to an exact geometry authority (a
    capture revision, a manual geometry record, an imported mesh ref);
    ``None`` means the space's geometry is explicitly UNKNOWN — a legal
    state that never substitutes another space's mesh. The theater itself
    is modeled as a ``kind='theater'`` space so single-room projects
    remain a degenerate site.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema: Literal['htdt.site-space'] = 'htdt.site-space'
    schema_version: Literal[1] = 1
    space_id: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    kind: SpaceKind
    label: str = Field(min_length=1)
    geometry_ref: str | None = None
    T_site_from_space: FrameTransform4x4 | None = None
    local_datum: str | None = None
    lifecycle: str = 'active'
    created_at_utc: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_space(self) -> 'SiteSpace':
        _require_iso8601(self.created_at_utc, 'created_at_utc')
        if not self.space_id.startswith(_SPACE_PREFIX):
            raise ValueError('space id must use space: prefix')
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('site space semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'schema': self.schema,
            'schema_version': self.schema_version,
            'space_id': self.space_id,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'kind': self.kind,
            'label': self.label,
            'lifecycle': self.lifecycle,
            'created_at_utc': self.created_at_utc,
            'evidence_refs': list(self.evidence_refs),
        }
        for key, value in (
            ('geometry_ref', self.geometry_ref),
            ('T_site_from_space', self.T_site_from_space),
            ('local_datum', self.local_datum),
        ):
            if value is not None:
                payload[key] = (
                    value.model_dump(mode='json')
                    if isinstance(value, BaseModel)
                    else value
                )
        return payload

    def authority_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.space_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


def register_space(
    *,
    document_id: str,
    kind: SpaceKind,
    label: str,
    geometry_ref: str | None = None,
    T_site_from_space: FrameTransform4x4 | None = None,
    local_datum: str | None = None,
    evidence_refs: tuple[str, ...] = (),
    space_id: str | None = None,
    authority_version: str = '1',
    created_at_utc: str | None = None,
) -> SiteSpace:
    payload: dict[str, Any] = {
        'space_id': space_id or f'{_SPACE_PREFIX}{uuid4()}',
        'authority_version': authority_version,
        'document_id': document_id,
        'kind': kind,
        'label': label,
        'geometry_ref': geometry_ref,
        'T_site_from_space': T_site_from_space,
        'local_datum': local_datum,
        'lifecycle': 'active',
        'created_at_utc': created_at_utc or _utc_now(),
        'evidence_refs': evidence_refs,
    }
    provisional = SiteSpace.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return SiteSpace.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


def retarget_space_transform(
    space: SiteSpace,
    *,
    T_site_from_space: FrameTransform4x4,
    authority_version: str,
) -> SiteSpace:
    """Re-aligning a space mints a NEW authority version — the historical
    alignment stays immutable under the previous version."""

    payload = space.model_dump(mode='python')
    payload.pop('semantic_sha256')
    payload['T_site_from_space'] = T_site_from_space
    payload['authority_version'] = authority_version
    provisional = SiteSpace.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return SiteSpace.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


class SpaceRelationship(BaseModel):
    """Explicit typed relationship between two spaces (#729).

    Connectivity is declared, never inferred: ``opening_portal`` and
    ``cable_route`` model explicit openings/routes; ``adjacent_boundary``
    a shared construction boundary; ``equipment_service`` and
    ``isolation_source_receiver`` keep their measurement/routing semantics.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema: Literal['htdt.space-relationship'] = 'htdt.space-relationship'
    schema_version: Literal[1] = 1
    relationship_id: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    kind: SpaceRelationshipKind
    space_a_id: str = Field(min_length=1)
    space_b_id: str = Field(min_length=1)
    detail: str = ''
    created_at_utc: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_relationship(self) -> 'SpaceRelationship':
        _require_iso8601(self.created_at_utc, 'created_at_utc')
        if not self.relationship_id.startswith(_REL_PREFIX):
            raise ValueError(
                'relationship id must use space-rel: prefix'
            )
        if self.space_a_id == self.space_b_id:
            raise ValueError('a space cannot relate to itself')
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('space relationship semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema': self.schema,
            'schema_version': self.schema_version,
            'relationship_id': self.relationship_id,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'kind': self.kind,
            'space_a_id': self.space_a_id,
            'space_b_id': self.space_b_id,
            'detail': self.detail,
            'created_at_utc': self.created_at_utc,
            'evidence_refs': list(self.evidence_refs),
        }

    def authority_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.relationship_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


def link_spaces(
    *,
    document_id: str,
    kind: SpaceRelationshipKind,
    space_a_id: str,
    space_b_id: str,
    detail: str = '',
    evidence_refs: tuple[str, ...] = (),
    relationship_id: str | None = None,
    authority_version: str = '1',
    created_at_utc: str | None = None,
) -> SpaceRelationship:
    payload: dict[str, Any] = {
        'relationship_id': relationship_id or f'{_REL_PREFIX}{uuid4()}',
        'authority_version': authority_version,
        'document_id': document_id,
        'kind': kind,
        'space_a_id': space_a_id,
        'space_b_id': space_b_id,
        'detail': detail,
        'created_at_utc': created_at_utc or _utc_now(),
        'evidence_refs': evidence_refs,
    }
    provisional = SpaceRelationship.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return SpaceRelationship.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


class SiteRepository:
    """Append-only site topology store on the shared cad DB (#729)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        ensure_native_schema(self.path)
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection,
                'cad_site_spaces',
                'cad_site_relationships',
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        return connection

    def save_space(self, space: SiteSpace) -> SiteSpace:
        """Append a space version; conflicting same-id content fails."""
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_site_spaces('
                'space_id, document_id, kind, authority_version, '
                'semantic_sha256, payload_json) VALUES(?,?,?,?,?,?) '
                'ON CONFLICT(space_id) DO NOTHING',
                (
                    space.space_id,
                    space.document_id,
                    space.kind,
                    space.authority_version,
                    space.semantic_sha256,
                    space.model_dump_json(),
                ),
            )
            row = connection.execute(
                'SELECT payload_json FROM cad_site_spaces WHERE space_id=?',
                (space.space_id,),
            ).fetchone()
            if row['payload_json'] != space.model_dump_json():
                raise ValueError(
                    f'site space {space.space_id} already persisted with '
                    'different content — retarget via a new authority '
                    'version instead'
                )
        return space

    def get_space(self, space_id: str) -> SiteSpace | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_site_spaces WHERE space_id=?',
                (space_id,),
            ).fetchone()
        if row is None:
            return None
        return SiteSpace.model_validate_json(row['payload_json'])

    def list_spaces(
        self, document_id: str, *, kind: SpaceKind | None = None
    ) -> tuple[SiteSpace, ...]:
        with closing(self._connect()) as connection:
            if kind is None:
                rows = connection.execute(
                    'SELECT payload_json FROM cad_site_spaces '
                    'WHERE document_id=? ORDER BY space_id ASC',
                    (document_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    'SELECT payload_json FROM cad_site_spaces '
                    'WHERE document_id=? AND kind=? ORDER BY space_id ASC',
                    (document_id, kind),
                ).fetchall()
        return tuple(
            SiteSpace.model_validate_json(row['payload_json'])
            for row in rows
        )

    def save_relationship(
        self, relationship: SpaceRelationship
    ) -> SpaceRelationship:
        with closing(self._connect()) as connection, connection:
            # Both endpoints must be registered spaces in this document —
            # relationships reference the site hierarchy, never free text.
            for endpoint in (
                relationship.space_a_id,
                relationship.space_b_id,
            ):
                found = connection.execute(
                    'SELECT 1 FROM cad_site_spaces '
                    'WHERE space_id=? AND document_id=?',
                    (endpoint, relationship.document_id),
                ).fetchone()
                if found is None:
                    raise ValueError(
                        f'relationship endpoint {endpoint} is not a '
                        f'registered space in {relationship.document_id}'
                    )
            connection.execute(
                'INSERT INTO cad_site_relationships('
                'relationship_id, document_id, kind, space_a_id, '
                'space_b_id, semantic_sha256, payload_json) '
                'VALUES(?,?,?,?,?,?,?) '
                'ON CONFLICT(relationship_id) DO NOTHING',
                (
                    relationship.relationship_id,
                    relationship.document_id,
                    relationship.kind,
                    relationship.space_a_id,
                    relationship.space_b_id,
                    relationship.semantic_sha256,
                    relationship.model_dump_json(),
                ),
            )
            row = connection.execute(
                'SELECT payload_json FROM cad_site_relationships '
                'WHERE relationship_id=?',
                (relationship.relationship_id,),
            ).fetchone()
            if row['payload_json'] != relationship.model_dump_json():
                raise ValueError(
                    f'space relationship {relationship.relationship_id} '
                    'already persisted with different content'
                )
        return relationship

    def get_relationship(
        self, relationship_id: str
    ) -> SpaceRelationship | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_site_relationships '
                'WHERE relationship_id=?',
                (relationship_id,),
            ).fetchone()
        if row is None:
            return None
        return SpaceRelationship.model_validate_json(row['payload_json'])

    def list_relationships_for_space(
        self, document_id: str, space_id: str
    ) -> tuple[SpaceRelationship, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_site_relationships '
                'WHERE document_id=? AND (space_a_id=? OR space_b_id=?) '
                'ORDER BY relationship_id ASC',
                (document_id, space_id, space_id),
            ).fetchall()
        return tuple(
            SpaceRelationship.model_validate_json(row['payload_json'])
            for row in rows
        )


__all__ = [
    'FrameTransform4x4',
    'IDENTITY_TRANSFORM',
    'SiteRepository',
    'SiteSpace',
    'SpaceKind',
    'SpaceRelationship',
    'SpaceRelationshipKind',
    'link_spaces',
    'register_space',
    'retarget_space_transform',
]
