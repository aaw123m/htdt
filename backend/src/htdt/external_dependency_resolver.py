"""External authority dependency resolver (#600).

Projects reference external authorities (Capture projects/libraries,
equipment catalogs, material libraries, target curves). Resolution must be
*exact*: an identifier either resolves to the same authority or it does not
resolve at all. There is no fuzzy matching, no "closest" substitution, and no
silent fallback onto a different artifact — a wrong local copy is an
``IDENTITY_CONFLICT``, not a resolution.

Resolution order is deterministic and recorded:

1. ``unsupported_dependency_kind`` — the resolver was not configured for
   this dependency kind (nothing was attempted);
2. ``resolved_exact_local`` — an exact ``(kind, authority_ref)`` hit in the
   local index;
3. ``resolved_embedded`` — an exact hit in project-embedded snapshots;
4. ``resolved_imported`` — an exact hit in the importable library index;
5. ``required_unresolved`` / ``optional_unresolved`` — no exact hit anywhere.

At every stage an ``expected_sha256`` pin is verified: a matching id with a
mismatching content hash downgrades to ``identity_conflict``. Every attempt
produces a persisted :class:`DependencyResolutionEvent` so later audits can
replay which authority the project actually consumed.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


DEPENDENCY_SCHEMA_VERSION = 1
DEPENDENCY_AUTHORITY_VERSION = 'external-dependency-1'
RESOLUTION_SCHEMA_VERSION = 1
RESOLUTION_AUTHORITY_VERSION = 'dependency-resolution-1'

#: Dependency kinds the resolver understands by default.
DependencyKind = Literal[
    'capture_project',
    'capture_authority',
    'equipment_catalog',
    'material_library',
    'target_curve',
    'other',
]

DependencyOutcome = Literal[
    'resolved_exact_local',
    'resolved_embedded',
    'resolved_imported',
    'optional_unresolved',
    'required_unresolved',
    'identity_conflict',
    'unsupported_dependency_kind',
]

ResolvedVia = Literal['local', 'embedded', 'imported']


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


class ExternalAuthorityDependency(BaseModel):
    """One exact external authority a project depends on."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = DEPENDENCY_SCHEMA_VERSION
    authority_version: Literal['external-dependency-1'] = (
        DEPENDENCY_AUTHORITY_VERSION
    )
    dependency_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    kind: DependencyKind
    authority_ref: str = Field(min_length=1)
    required: bool = True
    expected_sha256: str | None = Field(
        default=None, min_length=8, max_length=64
    )
    note: str | None = None
    created_at_utc: str = Field(min_length=1)
    dependency_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_dependency(self) -> 'ExternalAuthorityDependency':
        if self.dependency_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ExternalAuthorityDependency hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'dependency_id': self.dependency_id,
            'document_id': self.document_id,
            'kind': self.kind,
            'authority_ref': self.authority_ref,
            'required': self.required,
            'expected_sha256': self.expected_sha256,
            'note': self.note,
            'created_at_utc': self.created_at_utc,
        }


def build_external_dependency(
    *,
    document_id: str,
    kind: DependencyKind,
    authority_ref: str,
    created_at_utc: str,
    required: bool = True,
    expected_sha256: str | None = None,
    note: str | None = None,
    dependency_id: str | None = None,
) -> ExternalAuthorityDependency:
    payload: dict[str, Any] = {
        'dependency_id': dependency_id or str(uuid4()),
        'document_id': document_id,
        'kind': kind,
        'authority_ref': authority_ref,
        'required': required,
        'expected_sha256': expected_sha256,
        'note': note,
        'created_at_utc': created_at_utc,
    }
    provisional = ExternalAuthorityDependency.model_construct(
        **payload, dependency_sha256='0' * 64
    )
    return ExternalAuthorityDependency(
        **payload,
        dependency_sha256=_hash(provisional.semantic_payload()),
    )


class DependencyResolutionEvent(BaseModel):
    """Persisted record of one resolution attempt."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = RESOLUTION_SCHEMA_VERSION
    authority_version: Literal['dependency-resolution-1'] = (
        RESOLUTION_AUTHORITY_VERSION
    )
    event_id: str = Field(min_length=1)
    dependency_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    outcome: DependencyOutcome
    resolved_via: ResolvedVia | None = None
    resolved_sha256: str | None = Field(
        default=None, min_length=8, max_length=64
    )
    reason: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    event_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_event(self) -> 'DependencyResolutionEvent':
        resolved = self.outcome in (
            'resolved_exact_local',
            'resolved_embedded',
            'resolved_imported',
        )
        if resolved != (self.resolved_via is not None):
            raise ValueError(
                'resolved_via must be set exactly for resolved outcomes'
            )
        if self.event_sha256 != _hash(self.semantic_payload()):
            raise ValueError('DependencyResolutionEvent hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'event_id': self.event_id,
            'dependency_id': self.dependency_id,
            'document_id': self.document_id,
            'outcome': self.outcome,
            'resolved_via': self.resolved_via,
            'resolved_sha256': self.resolved_sha256,
            'reason': self.reason,
            'created_at_utc': self.created_at_utc,
        }


#: Indexes consulted by the resolver, in deterministic order. Every index is
#: a ``(kind, authority_ref) -> content sha256`` map — only exact id matches
#: are ever consulted.
class DependencyResolutionContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    supported_kinds: tuple[str, ...]
    local_index: Mapping[tuple[str, str], str] = {}
    embedded_index: Mapping[tuple[str, str], str] = {}
    import_index: Mapping[tuple[str, str], str] = {}


def _check(
    dependency: ExternalAuthorityDependency,
    index: Mapping[tuple[str, str], str],
    via: ResolvedVia,
    outcome: DependencyOutcome,
    resolved_at_utc: str,
) -> DependencyResolutionEvent | None:
    key = (dependency.kind, dependency.authority_ref)
    resolved = index.get(key)
    if resolved is None:
        return None
    if (
        dependency.expected_sha256 is not None
        and resolved != dependency.expected_sha256
    ):
        return _event(
            dependency,
            outcome='identity_conflict',
            resolved_via=None,
            resolved_sha256=resolved,
            reason=(
                f'{via} index matched the id but the content hash '
                'contradicts the pinned expectation'
            ),
            resolved_at_utc=resolved_at_utc,
        )
    return _event(
        dependency,
        outcome=outcome,
        resolved_via=via,
        resolved_sha256=resolved,
        reason=f'exact {via} match',
        resolved_at_utc=resolved_at_utc,
    )


def _event(
    dependency: ExternalAuthorityDependency,
    *,
    outcome: DependencyOutcome,
    resolved_via: ResolvedVia | None,
    resolved_sha256: str | None,
    reason: str,
    resolved_at_utc: str,
) -> DependencyResolutionEvent:
    payload: dict[str, Any] = {
        'event_id': str(uuid4()),
        'dependency_id': dependency.dependency_id,
        'document_id': dependency.document_id,
        'outcome': outcome,
        'resolved_via': resolved_via,
        'resolved_sha256': resolved_sha256,
        'reason': reason,
        'created_at_utc': resolved_at_utc,
    }
    provisional = DependencyResolutionEvent.model_construct(
        **payload, event_sha256='0' * 64
    )
    return DependencyResolutionEvent(
        **payload,
        event_sha256=_hash(provisional.semantic_payload()),
    )


def resolve_external_dependency(
    dependency: ExternalAuthorityDependency,
    context: DependencyResolutionContext,
    *,
    resolved_at_utc: str,
) -> DependencyResolutionEvent:
    """Resolve one dependency with exact matching only.

    The context's three indexes are consulted in a fixed order
    (local → embedded → imported); the first exact hit wins and every
    decision is returned as a persistable event.
    """

    if dependency.kind not in context.supported_kinds:
        return _event(
            dependency,
            outcome='unsupported_dependency_kind',
            resolved_via=None,
            resolved_sha256=None,
            reason='dependency kind is not supported by this resolver',
            resolved_at_utc=resolved_at_utc,
        )
    for index, via, outcome in (
        (context.local_index, 'local', 'resolved_exact_local'),
        (context.embedded_index, 'embedded', 'resolved_embedded'),
        (context.import_index, 'imported', 'resolved_imported'),
    ):
        event = _check(dependency, index, via, outcome, resolved_at_utc)
        if event is not None:
            return event
    return _event(
        dependency,
        outcome=(
            'required_unresolved'
            if dependency.required
            else 'optional_unresolved'
        ),
        resolved_via=None,
        resolved_sha256=None,
        reason='no exact match in local, embedded, or importable indexes',
        resolved_at_utc=resolved_at_utc,
    )


__all__ = [
    'DEPENDENCY_AUTHORITY_VERSION',
    'DEPENDENCY_SCHEMA_VERSION',
    'RESOLUTION_AUTHORITY_VERSION',
    'RESOLUTION_SCHEMA_VERSION',
    'DependencyKind',
    'DependencyOutcome',
    'DependencyResolutionContext',
    'DependencyResolutionEvent',
    'ExternalAuthorityDependency',
    'ResolvedVia',
    'build_external_dependency',
    'resolve_external_dependency',
]
