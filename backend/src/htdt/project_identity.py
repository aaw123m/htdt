from __future__ import annotations

import re
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


UUID4_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
)
PROJECT_IDENTITY_AUTHORITY = 'htdt-project-identity-1'


class ProjectIdentityError(ValueError):
    """Project identity could not be resolved or compared safely."""


class ProjectIdentityConflictError(ProjectIdentityError):
    """Two authority surfaces disagree about the same project identity.

    Raised for the hard-fail cases: the same ``project_id`` carrying
    structurally incompatible metadata, or a mission/bundle pinned to one
    project being routed to a destination for another. Display names are
    never used to resolve or excuse an identity conflict.
    """


def _uuid4(value: str) -> str:
    if not UUID4_RE.fullmatch(value):
        raise ValueError('value must be a lowercase UUIDv4')
    return value


class HTDTProjectReference(BaseModel):
    """Versioned stable project identity shared by Mission/handoff protocols.

    ``project_id`` is opaque, globally collision-resistant, immutable for the
    lifetime of one project lineage, and independent of display name, file
    path, data directory, or PC hostname. ``project_name``/``room_name`` are
    human presentation only — routing never keys on them.
    """

    model_config = ConfigDict(frozen=True, extra='forbid', serialize_by_alias=True)

    schema_: Literal['htdt.project-reference'] = Field(default='htdt.project-reference', alias='schema')
    schema_version: Literal[1] = 1
    identity_authority: Literal['htdt-project-identity-1'] = (
        PROJECT_IDENTITY_AUTHORITY
    )
    project_id: str
    document_id: str | None = Field(default=None, min_length=1)
    room_id: str | None = Field(default=None, min_length=1)
    project_name: str | None = Field(default=None, min_length=1)
    room_name: str | None = Field(default=None, min_length=1)
    cloned_from_project_id: str | None = None

    @field_validator('project_id', 'cloned_from_project_id')
    @classmethod
    def validate_uuid4(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return _uuid4(value)

    @model_validator(mode='after')
    def valid_reference(self) -> 'HTDTProjectReference':
        if (
            self.cloned_from_project_id is not None
            and self.cloned_from_project_id == self.project_id
        ):
            raise ValueError('cloned_from_project_id must differ from project_id')
        return self


class HTDTLegacyProjectRef(BaseModel):
    """Compatibility adapter result for a Task Plan v1 ``projectRef`` string.

    The legacy value is preserved verbatim as ``legacy_project_ref``. No
    modern project UUID is fabricated: without an exact established mapping,
    an arbitrary old string stays an unresolved legacy reference and routes
    through explicit staging/user mapping rather than silent reinterpretation.
    """

    model_config = ConfigDict(frozen=True, extra='forbid', serialize_by_alias=True)

    schema_: Literal['htdt.project-reference.legacy'] = Field(default='htdt.project-reference.legacy', alias='schema')
    schema_version: Literal[1] = 1
    legacy_project_ref: str = Field(min_length=1)


InboundProjectRef = HTDTProjectReference | HTDTLegacyProjectRef


class HTDTProjectDestination(BaseModel):
    """Receiver-routing record: which receiver destination serves a project.

    Kept deliberately separate from project identity itself — receiver or
    pairing/TLS identity never lives inside ``HTDTProjectReference``, and a
    routing record can change generation without mutating the project.
    """

    model_config = ConfigDict(frozen=True, extra='forbid', serialize_by_alias=True)

    schema_: Literal['htdt.project-destination'] = Field(default='htdt.project-destination', alias='schema')
    schema_version: Literal[1] = 1
    project: HTDTProjectReference
    receiver_instance_id: str
    destination_id: str
    routing_generation: int = Field(ge=0)

    @field_validator('receiver_instance_id', 'destination_id')
    @classmethod
    def validate_uuid4(cls, value: str) -> str:
        return _uuid4(value)


def new_project_reference(
    *,
    project_name: str | None = None,
    document_id: str | None = None,
    room_id: str | None = None,
    room_name: str | None = None,
) -> HTDTProjectReference:
    """Mint a fresh project identity (new project, or import-as-copy)."""

    return HTDTProjectReference(
        project_id=str(uuid4()),
        document_id=document_id,
        room_id=room_id,
        project_name=project_name,
        room_name=room_name,
    )


def duplicate_project_reference(
    source: HTDTProjectReference,
    *,
    document_id: str | None = None,
    project_name: str | None = None,
) -> HTDTProjectReference:
    """Duplicate-as-new-project: distinct identity with lineage.

    ``new_project_id != source_project_id`` and
    ``cloned_from_project_id == source_project_id``. Outstanding missions for
    the source keep resolving to the source identity; new missions originate
    from the clone. The optional ``document_id`` lets the caller rebind the
    duplicated document identity explicitly.
    """

    return HTDTProjectReference(
        project_id=str(uuid4()),
        document_id=document_id if document_id is not None else source.document_id,
        room_id=source.room_id,
        project_name=(
            project_name if project_name is not None else source.project_name
        ),
        room_name=source.room_name,
        cloned_from_project_id=source.project_id,
    )


def migrate_project_reference(
    source: HTDTProjectReference,
) -> HTDTProjectReference:
    """PC migration / disaster restore: identity is retained verbatim."""

    return source


def resolve_project_reference(value: object) -> InboundProjectRef:
    """Decode an inbound project reference from a wire payload.

    A structured mapping decodes as the versioned reference; a bare string is
    the Task Plan v1 ``projectRef`` form and is preserved verbatim as a legacy
    reference — never reinterpreted into a fabricated UUID.
    """

    if isinstance(value, HTDTProjectReference | HTDTLegacyProjectRef):
        return value
    if isinstance(value, str):
        return HTDTLegacyProjectRef(legacy_project_ref=value)
    if isinstance(value, dict):
        if value.get('schema') == 'htdt.project-reference.legacy':
            return HTDTLegacyProjectRef.model_validate(value)
        return HTDTProjectReference.model_validate(value)
    raise ProjectIdentityError(
        f'unsupported project reference payload: {type(value).__name__}'
    )


def assert_project_identity_compatible(
    issuing: HTDTProjectReference,
    candidate: HTDTProjectReference,
    *,
    context: str = 'project reference',
) -> None:
    """Hard-fail on structurally incompatible identity for one project_id.

    Display-name drift is allowed (renames must not break routing). Identity-
    bearing fields may not disagree for the same ``project_id``.
    """

    if issuing.project_id != candidate.project_id:
        raise ProjectIdentityConflictError(
            f'{context}: project identity mismatch '
            f'{issuing.project_id} != {candidate.project_id}'
        )
    for field_name in ('document_id', 'room_id'):
        left = getattr(issuing, field_name)
        right = getattr(candidate, field_name)
        if left is not None and right is not None and left != right:
            raise ProjectIdentityConflictError(
                f'{context}: incompatible {field_name} for project '
                f'{issuing.project_id}: {left} != {right}'
            )


def assert_destination_matches(
    pinned: HTDTProjectReference,
    destination: HTDTProjectDestination,
    *,
    context: str = 'project destination',
) -> None:
    """Route only when the destination serves the exact pinned project."""

    assert_project_identity_compatible(
        pinned,
        destination.project,
        context=context,
    )


def classify_project_reference(
    ref: InboundProjectRef,
    known: tuple[HTDTProjectReference, ...] | list[HTDTProjectReference],
) -> Literal[
    'exact_project_match',
    'known_project_lineage',
    'unknown_project_reference',
    'legacy_project_ref',
]:
    """Capture Inbox routing classification by exact identity, never name.

    - ``exact_project_match``: the project_id exists in ``known``.
    - ``known_project_lineage``: the project_id is unknown but descends from a
      known identity (cloned lineage) — a migrated/copy ancestor exists.
    - ``unknown_project_reference``: structured reference with no lineage to
      any known project.
    - ``legacy_project_ref``: unresolvable pre-contract string; stage
      unassigned without rewriting the origin identity.
    """

    if isinstance(ref, HTDTLegacyProjectRef):
        return 'legacy_project_ref'
    known_ids = {item.project_id for item in known}
    if ref.project_id in known_ids:
        return 'exact_project_match'
    if ref.cloned_from_project_id is not None and (
        ref.cloned_from_project_id in known_ids
    ):
        return 'known_project_lineage'
    return 'unknown_project_reference'
