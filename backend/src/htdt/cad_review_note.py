"""Bounded review annotations bound to exact authority (#730).

Per the collaboration product boundary: HTDT supports bounded exchange
and review — portable bundles, reports, staged field returns — and defers
real-time/cloud multi-user editing. A ``ReviewNote`` is the supported
review primitive: a human-authored note pinned to an exact authority
(SceneRevision, SystemVariant, commissioning deviation, design
alternative, installation evidence or project document), optionally
hashed, kept strictly separate from physical/measured truth.

Notes carry an author LABEL, not an account — no cloud identity,
mentions, presence, or merge semantics. A note never modifies the
authority it references.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_schema import ensure_native_schema, require_native_tables
from .r120_geometry_compiler import ExactExternalAuthorityRef
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash
from .clock import utc_now_iso as _utc_now


ReviewNoteSubjectKind = Literal[
    'scene_revision',
    'system_variant',
    'commissioning_deviation',
    'design_alternative',
    'installation_evidence',
    'project_document',
]

ReviewNoteResolution = Literal[
    'open',
    'accepted',
    'declined',
    'withdrawn',
]

_NOTE_PREFIX = 'review-note:'


def _require_iso8601(value: str, name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{name} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{name} must be timezone-aware')


class ReviewNote(BaseModel):
    """Immutable review annotation pinned to exact authority (#730).

    ``subject_sha256`` optionally pins the exact authority content being
    reviewed; reviewers label themselves with ``author_label`` — the
    model never requires an account or identity service.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema: Literal['htdt.review-note'] = 'htdt.review-note'
    schema_version: Literal[1] = 1
    note_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    subject_kind: ReviewNoteSubjectKind
    subject_ref: str = Field(min_length=1)
    subject_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    author_label: str = Field(min_length=1)
    body: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    resolution: ReviewNoteResolution = 'open'
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_note(self) -> 'ReviewNote':
        _require_iso8601(self.created_at_utc, 'created_at_utc')
        if not self.note_id.startswith(_NOTE_PREFIX):
            raise ValueError('note id must use review-note: prefix')
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('review note semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'schema': self.schema,
            'schema_version': self.schema_version,
            'note_id': self.note_id,
            'document_id': self.document_id,
            'subject_kind': self.subject_kind,
            'subject_ref': self.subject_ref,
            'author_label': self.author_label,
            'body': self.body,
            'created_at_utc': self.created_at_utc,
            'resolution': self.resolution,
        }
        if self.subject_sha256 is not None:
            payload['subject_sha256'] = self.subject_sha256
        return payload

    def authority_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.note_id,
            authority_version='1',
            semantic_hash_sha256=self.semantic_sha256,
        )


def add_review_note(
    *,
    document_id: str,
    subject_kind: ReviewNoteSubjectKind,
    subject_ref: str,
    author_label: str,
    body: str,
    subject_sha256: str | None = None,
    resolution: ReviewNoteResolution = 'open',
    note_id: str | None = None,
    created_at_utc: str | None = None,
) -> ReviewNote:
    payload: dict[str, Any] = {
        'note_id': note_id or f'{_NOTE_PREFIX}{uuid4()}',
        'document_id': document_id,
        'subject_kind': subject_kind,
        'subject_ref': subject_ref,
        'subject_sha256': subject_sha256,
        'author_label': author_label,
        'body': body,
        'created_at_utc': created_at_utc or _utc_now(),
        'resolution': resolution,
    }
    provisional = ReviewNote.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return ReviewNote.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


def resolve_review_note(
    note: ReviewNote, resolution: ReviewNoteResolution
) -> ReviewNote:
    """A resolution change mints a new immutable note state — the note
    itself never edits the authority it reviews."""
    payload = note.model_dump(mode='python')
    payload.pop('semantic_sha256')
    payload['resolution'] = resolution
    provisional = ReviewNote.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return ReviewNote.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


class ReviewNoteRepository:
    """Append-only review note store on the shared cad DB (#730)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        ensure_native_schema(self.path)
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection,
                'cad_review_notes',
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        return connection

    def save_note(self, note: ReviewNote) -> ReviewNote:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_review_notes('
                'note_id, document_id, subject_kind, subject_ref, '
                'resolution, created_at_utc, semantic_sha256, payload_json)'
                ' VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(note_id) DO UPDATE SET'
                ' resolution=excluded.resolution,'
                ' semantic_sha256=excluded.semantic_sha256,'
                ' payload_json=excluded.payload_json',
                (
                    note.note_id,
                    note.document_id,
                    note.subject_kind,
                    note.subject_ref,
                    note.resolution,
                    note.created_at_utc,
                    note.semantic_sha256,
                    note.model_dump_json(),
                ),
            )
            row = connection.execute(
                'SELECT payload_json FROM cad_review_notes WHERE note_id=?',
                (note.note_id,),
            ).fetchone()
            if row['payload_json'] != note.model_dump_json():
                raise ValueError(
                    f'review note {note.note_id} already persisted with '
                    'different content — notes are immutable except for '
                    'their resolution'
                )
        return note

    def get_note(self, note_id: str) -> ReviewNote | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_review_notes WHERE note_id=?',
                (note_id,),
            ).fetchone()
        if row is None:
            return None
        return ReviewNote.model_validate_json(row['payload_json'])

    def list_notes_for_subject(
        self, document_id: str, subject_ref: str
    ) -> tuple[ReviewNote, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_review_notes '
                'WHERE document_id=? AND subject_ref=? '
                'ORDER BY created_at_utc ASC, note_id ASC',
                (document_id, subject_ref),
            ).fetchall()
        return tuple(
            ReviewNote.model_validate_json(row['payload_json'])
            for row in rows
        )


__all__ = [
    'ReviewNote',
    'ReviewNoteRepository',
    'ReviewNoteResolution',
    'ReviewNoteSubjectKind',
    'add_review_note',
    'resolve_review_note',
]
