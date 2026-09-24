"""External authority dependency resolver (#600).

Projects reference external authorities (Capture projects/libraries,
equipment catalogs, material libraries, target curves). Resolution must be
*exact*: an identifier resolves to the same authority — id *and* pinned
semantic hash — or it does not resolve at all. There is no fuzzy matching,
no "closest" substitution, and no silent fallback onto a different
artifact.

Identity scheme is declared per dependency kind
(:data:`DEPENDENCY_KIND_IDENTITY`):

- **hash-bearing kinds** (the default for every built-in kind) require
  ``expected_sha256`` at build time — an ID-only dependency can never claim
  ``resolved_exact_*`` because it cannot be declared;
- **``other``** may omit the pin for legacy refs; an ID-only index hit then
  reports ``legacy_unverified``, never a resolved outcome. A pinned hash
  still yields a normal exact resolution;
- ``authority_version_id`` optionally carries the referenced authority's
  schema/version label where the owning authority defines one, so the same
  ``(id, hash)`` cannot be re-interpreted across schema revisions.

Resolution order is deterministic and recorded:

1. ``unsupported_dependency_kind`` — the resolver was not configured for
   this dependency kind (nothing was attempted);
2. ``resolved_exact_local`` — an exact ``(kind, authority_ref)`` +
   pinned-hash hit in the local index;
3. ``resolved_embedded`` — the same hit in project-embedded snapshots;
4. ``resolved_imported`` — the same hit in the importable library index;
5. ``required_unresolved`` / ``optional_unresolved`` — no hit anywhere;
6. ``identity_conflict`` — indexes were exhausted and at least one source
   held the same id with a contradicting content hash;
7. ``legacy_unverified`` — an ID-only hit on a kind with no hash pin.

**Fallback policy is explicit**: when one index holds the same id with a
contradicting hash, resolution keeps searching later indexes for the
pinned exact hash — a portable project can recover the exact authority
from an embedded/importable copy despite a stale local row — and records
which sources contradicted the pin in the event reason. When no index
satisfies the pin, the outcome is ``identity_conflict`` listing every
contradicting source rather than a plain miss.

Every attempt produces a :class:`DependencyResolutionEvent` intended to be
persisted by the repository's derived ``resolve_and_record`` path, so the
stored history is always the resolver's own output — never a caller-shaped
claim.
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


class DependencyKindIdentity(BaseModel):
    """Which identity fields a dependency kind requires.

    ``hash_required`` kinds cannot be declared without the authority's
    exact semantic hash; ``version_required`` kinds must also pin the
    authority's schema/version identity.
    """

    model_config = ConfigDict(frozen=True)

    hash_required: bool = True
    version_required: bool = False


#: The declared identity contract for each dependency kind.
DEPENDENCY_KIND_IDENTITY: Mapping[str, DependencyKindIdentity] = {
    'capture_project': DependencyKindIdentity(),
    'capture_authority': DependencyKindIdentity(),
    'equipment_catalog': DependencyKindIdentity(version_required=True),
    'material_library': DependencyKindIdentity(),
    'target_curve': DependencyKindIdentity(),
    # ``other`` tolerates legacy ID-only refs; they resolve to an explicit
    # legacy_unverified state instead of pretending to be exact.
    'other': DependencyKindIdentity(hash_required=False),
}


def dependency_kind_identity(kind: str) -> DependencyKindIdentity:
    """The declared identity contract for a kind; unknown kinds hash-pin."""

    return DEPENDENCY_KIND_IDENTITY.get(kind, DependencyKindIdentity())


DependencyOutcome = Literal[
    'resolved_exact_local',
    'resolved_embedded',
    'resolved_imported',
    'optional_unresolved',
    'required_unresolved',
    'identity_conflict',
    'legacy_unverified',
    'unsupported_dependency_kind',
]

ResolvedVia = Literal['local', 'embedded', 'imported']

RESOLVED_OUTCOMES: frozenset[str] = frozenset(
    {'resolved_exact_local', 'resolved_embedded', 'resolved_imported'}
)


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
    #: The referenced authority's schema/version identity where the owning
    #: authority defines one — retained so an ``(id, hash)`` pair cannot be
    #: silently re-interpreted under a different schema revision.
    authority_version_id: str | None = Field(default=None, min_length=1)
    required: bool = True
    expected_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    note: str | None = None
    created_at_utc: str = Field(min_length=1)
    dependency_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_dependency(self) -> 'ExternalAuthorityDependency':
        identity = dependency_kind_identity(self.kind)
        if identity.hash_required and self.expected_sha256 is None:
            raise ValueError(
                f'hash-bearing dependency kind {self.kind!r} requires '
                'expected_sha256 — declare the pin or use an explicit '
                'legacy kind'
            )
        if identity.version_required and self.authority_version_id is None:
            raise ValueError(
                f'dependency kind {self.kind!r} requires '
                'authority_version_id'
            )
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
            'authority_version_id': self.authority_version_id,
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
    authority_version_id: str | None = None,
    note: str | None = None,
    dependency_id: str | None = None,
) -> ExternalAuthorityDependency:
    payload: dict[str, Any] = {
        'dependency_id': dependency_id or str(uuid4()),
        'document_id': document_id,
        'kind': kind,
        'authority_ref': authority_ref,
        'authority_version_id': authority_version_id,
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
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    reason: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    event_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_event(self) -> 'DependencyResolutionEvent':
        resolved = self.outcome in RESOLVED_OUTCOMES
        if resolved != (self.resolved_via is not None):
            raise ValueError(
                'resolved_via must be set exactly for resolved outcomes'
            )
        if resolved and self.resolved_sha256 is None:
            raise ValueError('resolved outcomes require resolved_sha256')
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


def _event(
    dependency: ExternalAuthorityDependency,
    *,
    outcome: DependencyOutcome,
    resolved_via: ResolvedVia | None,
    resolved_sha256: str | None,
    reason: str,
    resolved_at_utc: str,
    event_id: str | None = None,
) -> DependencyResolutionEvent:
    payload: dict[str, Any] = {
        'event_id': event_id or str(uuid4()),
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
    event_id: str | None = None,
) -> DependencyResolutionEvent:
    """Resolve one dependency with exact matching only.

    The context's three indexes are consulted in a fixed order
    (local → embedded → imported). A source that holds the same id with a
    contradicting hash does not stop the search — the resolver keeps
    looking for the pinned exact hash in later indexes — and every
    contradicting source is named in the event's reason so audits can see
    where the conflict was.
    """

    if dependency.kind not in context.supported_kinds:
        return _event(
            dependency,
            outcome='unsupported_dependency_kind',
            resolved_via=None,
            resolved_sha256=None,
            reason='dependency kind is not supported by this resolver',
            resolved_at_utc=resolved_at_utc,
            event_id=event_id,
        )
    key = (dependency.kind, dependency.authority_ref)
    conflicts: list[str] = []
    for index, via, outcome in (
        (context.local_index, 'local', 'resolved_exact_local'),
        (context.embedded_index, 'embedded', 'resolved_embedded'),
        (context.import_index, 'imported', 'resolved_imported'),
    ):
        resolved = index.get(key)
        if resolved is None:
            continue
        if (
            dependency.expected_sha256 is not None
            and resolved != dependency.expected_sha256
        ):
            conflicts.append(via)
            continue
        if dependency.expected_sha256 is None:
            # An ID-only hit on a kind with no hash pin is an explicit
            # legacy/unverified state, never an exact resolution.
            return _event(
                dependency,
                outcome='legacy_unverified',
                resolved_via=None,
                resolved_sha256=resolved,
                reason=(
                    f'{via} index matched the id but the dependency '
                    'carries no hash pin — recorded as unverified, not '
                    'resolved'
                ),
                resolved_at_utc=resolved_at_utc,
                event_id=event_id,
            )
        reason = f'exact {via} match'
        if conflicts:
            reason += (
                '; contradicting hash in ' + ', '.join(conflicts) +
                ' index was bypassed for the pinned authority'
            )
        return _event(
            dependency,
            outcome=outcome,
            resolved_via=via,
            resolved_sha256=resolved,
            reason=reason,
            resolved_at_utc=resolved_at_utc,
            event_id=event_id,
        )
    if conflicts:
        return _event(
            dependency,
            outcome='identity_conflict',
            resolved_via=None,
            resolved_sha256=None,
            reason=(
                'id matched in ' + ', '.join(conflicts) +
                ' index but the content hash contradicts the pinned '
                'expectation; no exact copy found'
            ),
            resolved_at_utc=resolved_at_utc,
            event_id=event_id,
        )
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
        event_id=event_id,
    )


__all__ = [
    'DEPENDENCY_AUTHORITY_VERSION',
    'DEPENDENCY_KIND_IDENTITY',
    'DEPENDENCY_SCHEMA_VERSION',
    'RESOLUTION_AUTHORITY_VERSION',
    'RESOLUTION_SCHEMA_VERSION',
    'RESOLVED_OUTCOMES',
    'DependencyKind',
    'DependencyKindIdentity',
    'DependencyOutcome',
    'DependencyResolutionContext',
    'DependencyResolutionEvent',
    'ExternalAuthorityDependency',
    'ResolvedVia',
    'build_external_dependency',
    'dependency_kind_identity',
    'resolve_external_dependency',
]
