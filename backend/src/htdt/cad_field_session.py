"""Field session authority — the bounded mobile field companion (#728).

A ``FieldSession`` is the scoped, expiring pairing between one exact HTDT
project state and a field device: it pins the document, the design
SceneRevision / SystemVariant / CommissioningPlan it was issued against,
the entity/check scope it may cover, and its lifecycle state. Field
evidence returns through the #674 ``.htdtfieldreturn`` staging authority —
this module owns the session identity, the per-record evidence contract,
and the staleness rule: a session bound to a superseded design revision is
``stale`` and must reconcile explicitly rather than silently write into a
different current design.

The companion is a bounded field surface — never a second project
authority. Evidence records are immutable, carry exact session identity,
and stay ``pending_review`` until desktop reconciliation.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_schema import ensure_native_schema, require_native_tables
from .r120_geometry_compiler import ExactExternalAuthorityRef


FieldSessionTaskKind = Literal[
    'commissioning_checklist',
    'equipment_verification',
    'cable_verification',
    'termination_verification',
    'measurement_guidance',
    'general_survey',
]

FieldSessionState = Literal[
    'active',
    'returned',
    'stale',
    'expired',
    'revoked',
]

FieldSessionFreshness = Literal[
    'current',
    'stale',
    'expired',
    'revoked',
    'returned',
]

FieldEvidenceKind = Literal[
    'photo',
    'note',
    'numeric_measurement',
    'checklist_response',
    'serial_capture',
    'termination_check',
]

FieldEvidenceReviewState = Literal[
    'pending_review',
    'accepted',
    'rejected',
]

_SESSION_PREFIX = 'field-session:'
_EVIDENCE_PREFIX = 'field-evidence:'


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: dict[str, Any]) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


def _require_iso8601(value: str, name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{name} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{name} must be timezone-aware')


class FieldSession(BaseModel):
    """Sealed scoped pairing for one exact project state (#728).

    ``scope_refs`` are the exact entity/check/task identities the session
    may cover — a scan or record outside the scope is rejected at review,
    never fuzzy-matched. ``revision_id`` pins the design the session was
    issued against; returning against a different head marks the session
    stale.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema: Literal['htdt.field-session'] = 'htdt.field-session'
    schema_version: Literal[1] = 1
    session_id: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    project_id: str | None = None
    revision_id: str | None = None
    system_variant_id: str | None = None
    commissioning_plan_id: str | None = None
    task_kind: FieldSessionTaskKind = 'general_survey'
    scope_refs: tuple[str, ...] = ()
    build_identity: str = Field(min_length=1)
    issued_at_utc: str = Field(min_length=1)
    expires_at_utc: str | None = None
    returned_at_utc: str | None = None
    state: FieldSessionState = 'active'
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_session(self) -> 'FieldSession':
        _require_iso8601(self.issued_at_utc, 'issued_at_utc')
        if self.expires_at_utc is not None:
            _require_iso8601(self.expires_at_utc, 'expires_at_utc')
            if self.expires_at_utc <= self.issued_at_utc:
                raise ValueError(
                    'expires_at_utc must follow issued_at_utc'
                )
        if self.returned_at_utc is not None:
            _require_iso8601(self.returned_at_utc, 'returned_at_utc')
            if self.state != 'returned':
                raise ValueError(
                    'returned_at_utc is only valid in state returned'
                )
        if self.state != 'returned' and self.returned_at_utc is not None:
            raise ValueError('returned_at_utc requires returned state')
        if not self.session_id.startswith(_SESSION_PREFIX):
            raise ValueError(
                'session id must use field-session: prefix'
            )
        if len(set(self.scope_refs)) != len(self.scope_refs) or any(
            not item for item in self.scope_refs
        ):
            raise ValueError('scope_refs must be non-empty and unique')
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('field session semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'schema': self.schema,
            'schema_version': self.schema_version,
            'session_id': self.session_id,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'task_kind': self.task_kind,
            'scope_refs': list(self.scope_refs),
            'build_identity': self.build_identity,
            'issued_at_utc': self.issued_at_utc,
            'state': self.state,
        }
        for key, value in (
            ('project_id', self.project_id),
            ('revision_id', self.revision_id),
            ('system_variant_id', self.system_variant_id),
            ('commissioning_plan_id', self.commissioning_plan_id),
            ('expires_at_utc', self.expires_at_utc),
            ('returned_at_utc', self.returned_at_utc),
        ):
            if value is not None:
                payload[key] = value
        return payload

    def authority_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.session_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


def issue_field_session(
    *,
    document_id: str,
    build_identity: str,
    project_id: str | None = None,
    revision_id: str | None = None,
    system_variant_id: str | None = None,
    commissioning_plan_id: str | None = None,
    task_kind: FieldSessionTaskKind = 'general_survey',
    scope_refs: tuple[str, ...] = (),
    expires_at_utc: str | None = None,
    session_id: str | None = None,
    authority_version: str = '1',
    issued_at_utc: str | None = None,
) -> FieldSession:
    """Issue a sealed field session bound to exact project authority."""

    payload: dict[str, Any] = {
        'session_id': session_id or f'{_SESSION_PREFIX}{uuid4()}',
        'authority_version': authority_version,
        'document_id': document_id,
        'project_id': project_id,
        'revision_id': revision_id,
        'system_variant_id': system_variant_id,
        'commissioning_plan_id': commissioning_plan_id,
        'task_kind': task_kind,
        'scope_refs': scope_refs,
        'build_identity': build_identity,
        'issued_at_utc': issued_at_utc or _utc_now(),
        'expires_at_utc': expires_at_utc,
        'returned_at_utc': None,
        'state': 'active',
    }
    provisional = FieldSession.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return FieldSession.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


def _session_copy(session: FieldSession, **updates: Any) -> FieldSession:
    payload = session.model_dump(mode='python')
    payload.pop('semantic_sha256')
    payload.update(updates)
    provisional = FieldSession.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return FieldSession.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


def evaluate_field_session_freshness(
    session: FieldSession,
    *,
    current_revision_id: str | None,
    now_utc: str | None = None,
) -> FieldSessionFreshness:
    """Return the session's freshness against the current design (#728).

    Order matters: revoked/returned are terminal; expiry is checked before
    staleness so a lapsed session is reported as expired even if the design
    also moved.
    """

    if session.state == 'revoked':
        return 'revoked'
    if session.state == 'returned':
        return 'returned'
    now = now_utc or _utc_now()
    if session.expires_at_utc is not None and now > session.expires_at_utc:
        return 'expired'
    if (
        session.revision_id is not None
        and current_revision_id is not None
        and session.revision_id != current_revision_id
    ):
        return 'stale'
    return 'current'


def transition_field_session(
    session: FieldSession,
    state: FieldSessionState,
    *,
    at_utc: str | None = None,
) -> FieldSession:
    """Lifecycle transition preserving the pinned binding (#728)."""

    updates: dict[str, Any] = {'state': state}
    if state == 'returned':
        updates['returned_at_utc'] = at_utc or _utc_now()
    return _session_copy(session, **updates)


class FieldEvidenceRecord(BaseModel):
    """One immutable field-captured record (#728).

    ``value`` semantics follow ``kind``: a photo carries its content hash,
    a numeric measurement its reading, a checklist response its answer,
    a serial capture the operator-confirmed text. ``review_state`` starts
    at ``pending_review`` — field records never become project truth
    without desktop reconciliation.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema: Literal['htdt.field-evidence'] = 'htdt.field-evidence'
    schema_version: Literal[1] = 1
    record_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    kind: FieldEvidenceKind
    subject_ref: str | None = None
    captured_at_utc: str = Field(min_length=1)
    value_text: str | None = None
    value_numeric: float | None = None
    source_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    review_state: FieldEvidenceReviewState = 'pending_review'
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_record(self) -> 'FieldEvidenceRecord':
        _require_iso8601(self.captured_at_utc, 'captured_at_utc')
        if not self.record_id.startswith(_EVIDENCE_PREFIX):
            raise ValueError(
                'record id must use field-evidence: prefix'
            )
        if self.kind == 'numeric_measurement' and self.value_numeric is None:
            raise ValueError(
                'numeric_measurement requires value_numeric'
            )
        if self.kind == 'photo' and self.source_sha256 is None:
            raise ValueError('photo requires source_sha256')
        if self.kind in ('checklist_response', 'serial_capture') and (
            self.value_text is None
        ):
            raise ValueError(
                f'{self.kind} requires the operator-entered value_text'
            )
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('field evidence semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'schema': self.schema,
            'schema_version': self.schema_version,
            'record_id': self.record_id,
            'session_id': self.session_id,
            'kind': self.kind,
            'captured_at_utc': self.captured_at_utc,
            'review_state': self.review_state,
        }
        for key, value in (
            ('subject_ref', self.subject_ref),
            ('value_text', self.value_text),
            ('value_numeric', self.value_numeric),
            ('source_sha256', self.source_sha256),
        ):
            if value is not None:
                payload[key] = value
        return payload

    def authority_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.record_id,
            authority_version='1',
            semantic_hash_sha256=self.semantic_sha256,
        )


def record_field_evidence(
    session: FieldSession,
    *,
    kind: FieldEvidenceKind,
    subject_ref: str | None = None,
    value_text: str | None = None,
    value_numeric: float | None = None,
    source_sha256: str | None = None,
    captured_at_utc: str | None = None,
    record_id: str | None = None,
) -> FieldEvidenceRecord:
    """Capture one scoped evidence record against an issued session.

    A subject outside the session's declared scope is refused outright —
    the companion never widens its own authority.
    """

    if subject_ref is not None and session.scope_refs:
        if subject_ref not in session.scope_refs:
            raise ValueError(
                f'subject {subject_ref} is outside field session scope'
            )
    payload: dict[str, Any] = {
        'record_id': record_id or f'{_EVIDENCE_PREFIX}{uuid4()}',
        'session_id': session.session_id,
        'kind': kind,
        'subject_ref': subject_ref,
        'captured_at_utc': captured_at_utc or _utc_now(),
        'value_text': value_text,
        'value_numeric': value_numeric,
        'source_sha256': source_sha256,
        'review_state': 'pending_review',
    }
    provisional = FieldEvidenceRecord.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return FieldEvidenceRecord.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


class FieldSessionRepository:
    """Append-only field session + evidence store on the shared cad DB."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        ensure_native_schema(self.path)
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection,
                'cad_field_sessions',
                'cad_field_evidence_records',
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        return connection

    def save_session(self, session: FieldSession) -> FieldSession:
        """Persist a session; a same-id conflicting write fails closed."""
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_field_sessions('
                'session_id, document_id, task_kind, state, issued_at_utc, '
                'semantic_sha256, payload_json) VALUES(?,?,?,?,?,?,?) '
                'ON CONFLICT(session_id) DO UPDATE SET '
                ' state=excluded.state,'
                ' semantic_sha256=excluded.semantic_sha256,'
                ' payload_json=excluded.payload_json '
                'WHERE cad_field_sessions.document_id=excluded.document_id',
                (
                    session.session_id,
                    session.document_id,
                    session.task_kind,
                    session.state,
                    session.issued_at_utc,
                    session.semantic_sha256,
                    session.model_dump_json(),
                ),
            )
            row = connection.execute(
                'SELECT payload_json FROM cad_field_sessions '
                'WHERE session_id=?',
                (session.session_id,),
            ).fetchone()
            if row['payload_json'] != session.model_dump_json():
                raise ValueError(
                    f'field session {session.session_id} already persisted '
                    'with different content — sessions are immutable except '
                    'for their lifecycle state'
                )
        return session

    def get_session(self, session_id: str) -> FieldSession | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_field_sessions '
                'WHERE session_id=?',
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return FieldSession.model_validate_json(row['payload_json'])

    def list_sessions_for_document(
        self, document_id: str
    ) -> tuple[FieldSession, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_field_sessions '
                'WHERE document_id=? '
                'ORDER BY issued_at_utc ASC, session_id ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            FieldSession.model_validate_json(row['payload_json'])
            for row in rows
        )

    def save_evidence(
        self, record: FieldEvidenceRecord
    ) -> FieldEvidenceRecord:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_field_evidence_records('
                'record_id, session_id, kind, review_state, '
                'captured_at_utc, semantic_sha256, payload_json) '
                'VALUES(?,?,?,?,?,?,?) ON CONFLICT(record_id) DO NOTHING',
                (
                    record.record_id,
                    record.session_id,
                    record.kind,
                    record.review_state,
                    record.captured_at_utc,
                    record.semantic_sha256,
                    record.model_dump_json(),
                ),
            )
            row = connection.execute(
                'SELECT payload_json FROM cad_field_evidence_records '
                'WHERE record_id=?',
                (record.record_id,),
            ).fetchone()
            if row['payload_json'] != record.model_dump_json():
                raise ValueError(
                    f'field evidence {record.record_id} already persisted '
                    'with different content — records are immutable'
                )
        return record

    def get_evidence(
        self, record_id: str
    ) -> FieldEvidenceRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_field_evidence_records '
                'WHERE record_id=?',
                (record_id,),
            ).fetchone()
        if row is None:
            return None
        return FieldEvidenceRecord.model_validate_json(row['payload_json'])

    def list_evidence_for_session(
        self, session_id: str
    ) -> tuple[FieldEvidenceRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_field_evidence_records '
                'WHERE session_id=? '
                'ORDER BY captured_at_utc ASC, record_id ASC',
                (session_id,),
            ).fetchall()
        return tuple(
            FieldEvidenceRecord.model_validate_json(row['payload_json'])
            for row in rows
        )


__all__ = [
    'FieldEvidenceKind',
    'FieldEvidenceRecord',
    'FieldEvidenceReviewState',
    'FieldSession',
    'FieldSessionFreshness',
    'FieldSessionRepository',
    'FieldSessionState',
    'FieldSessionTaskKind',
    'evaluate_field_session_freshness',
    'issue_field_session',
    'record_field_evidence',
    'transition_field_session',
]
