"""Presentation-session authority — local/offline client review (#534).

A ``PresentationSession`` is the persisted, deterministic contract for a
client-facing walkthrough: it pins an exact ``SceneRevision`` (id +
content hash), optionally a ``SystemVariant`` and/or a
``DesignComparisonSet``, an ordered list of named viewpoints (camera +
view state, never a live camera pointer), ordered sections binding those
viewpoints to annotations, and evidence references — all sealed by a
semantic hash so a reopened session replays the identical presentation.

Scope kept per the issue contract:

- viewpoints are records — the session never holds a live VTK camera;
- sessions are append-only manifests of references, never copies of
  engineering state;
- presentation annotations stay presentation state — anything that would
  change engineering is represented as a ``PresentationProposal`` whose
  ``variant_candidate`` kind must pin a real ``SystemVariant``, or as a
  ``DesignDecisionRecord``/``ReviewNote`` — the session itself never
  mutates a scene;
- ``SynchronizedReviewBinding`` pairs two exact authorities (session or
  comparison alternative) for lockstep A/B review without merging any
  state;
- hosted multi-user collaboration and VR are explicitly out of scope.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_design_comparison import ComparisonEvidenceRef
from .cad_view_state import RoomCameraState, SectionPlaneState
from .canonical_json import canonical_sha256 as _hash


PRESENTATION_SESSION_SCHEMA_VERSION = 1
PRESENTATION_SESSION_AUTHORITY_VERSION = 'presentation-session-1'
PRESENTATION_BINDING_AUTHORITY_VERSION = 'presentation-sync-binding-1'
PRESENTATION_PROPOSAL_AUTHORITY_VERSION = 'presentation-proposal-1'

#: Watermark label a presentation advertises. Everything below
#: 'as_built' is a proposal — honest capability labeling, never
#: presenting a proposed design as built or client-approved.
PresentationStatusLabel = Literal[
    'draft',
    'proposed',
    'accepted',
    'as_built',
    'superseded',
]

#: Named overlay a viewpoint pins. The surface resolves availability per
#: overlay at replay time — an unknown/missing overlay degrades to
#: 'none' on display and stays recorded here verbatim.
PresentationOverlay = Literal[
    'none',
    'grid',
    'labels',
    'speaker_coverage',
    'sightlines',
    'measurement_points',
]

SyncSideKind = Literal['presentation_session', 'comparison_alternative']

ProposalKind = Literal[
    # The client picked/wants an engineering alternative: the proposal
    # pins a real SystemVariant — a tracked candidate, not a mutation.
    'variant_candidate',
    # Presentation-only decoration (a label, a shown/hidden entity,
    # a camera move). Never creates engineering state.
    'annotation_only',
]


class PresentationViewpoint(BaseModel):
    """One named, serializable, replayable viewpoint in a session."""

    model_config = ConfigDict(frozen=True)

    viewpoint_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    camera: RoomCameraState
    section: SectionPlaneState | None = None
    hidden_ids: tuple[str, ...] | None = None
    focus_entity_id: str | None = Field(default=None, min_length=1)
    overlay: str = 'none'
    note: str | None = None
    viewpoint_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_viewpoint(self) -> 'PresentationViewpoint':
        if self.viewpoint_sha256 != _hash(self.semantic_payload()):
            raise ValueError('PresentationViewpoint hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'viewpoint_id': self.viewpoint_id,
            'name': self.name,
            'camera': self.camera.model_dump(mode='json'),
            'section': (
                None if self.section is None else self.section.model_dump(mode='json')
            ),
            'hidden_ids': None if self.hidden_ids is None else list(self.hidden_ids),
            'focus_entity_id': self.focus_entity_id,
            'overlay': self.overlay,
            'note': self.note,
        }


class PresentationAnnotation(BaseModel):
    """Presentation-only annotation pinned inside a session.

    Anchored to a viewpoint and/or an entity id of the pinned revision;
    the annotation is fixed at session-build time — later comments go
    through ``ReviewNote`` so the session stays immutable.
    """

    model_config = ConfigDict(frozen=True)

    annotation_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    viewpoint_id: str | None = Field(default=None, min_length=1)
    entity_id: str | None = Field(default=None, min_length=1)
    author_label: str | None = Field(default=None, min_length=1)


class PresentationSection(BaseModel):
    """One ordered beat of the session — a viewpoint plus its caption."""

    model_config = ConfigDict(frozen=True)

    section_id: str = Field(min_length=1)
    title: str | None = None
    viewpoint_id: str = Field(min_length=1)
    annotation: str | None = None


class PresentationRenderSettings(BaseModel):
    """Recorded render intent for offline package builds.

    Fields describe what the package builder is asked to do; what it
    actually produced is declared per-row in the package manifest —
    settings are intent, not a capability claim.
    """

    model_config = ConfigDict(frozen=True)

    image_width_px: int = Field(default=1920, ge=64, le=8192)
    image_height_px: int = Field(default=1080, ge=64, le=8192)
    #: Yaw step between perspective renders in the 360 surrogate. None
    #: disables yaw stepping (each viewpoint renders once at its camera).
    yaw_step_deg: int | None = Field(default=None, ge=1, le=180)
    image_format: Literal['png'] = 'png'


class PresentationSession(BaseModel):
    """Immutable presentation manifest bound to exact scene authority."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = PRESENTATION_SESSION_SCHEMA_VERSION
    authority_version: Literal['presentation-session-1'] = (
        PRESENTATION_SESSION_AUTHORITY_VERSION
    )
    session_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    system_variant_id: str | None = Field(default=None, min_length=1)
    system_variant_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    comparison_set_id: str | None = Field(default=None, min_length=1)
    comparison_set_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    status_label: PresentationStatusLabel = 'draft'
    viewpoints: tuple[PresentationViewpoint, ...] = Field(min_length=1)
    sections: tuple[PresentationSection, ...] = ()
    annotations: tuple[PresentationAnnotation, ...] = ()
    evidence_refs: tuple[ComparisonEvidenceRef, ...] = ()
    render: PresentationRenderSettings = PresentationRenderSettings()
    author: str | None = Field(default=None, min_length=1)
    created_at_utc: str = Field(min_length=1)
    session_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_session(self) -> 'PresentationSession':
        variant_pair = (
            self.system_variant_id is not None,
            self.system_variant_sha256 is not None,
        )
        if variant_pair[0] != variant_pair[1]:
            raise ValueError('system variant id/hash must be supplied together')
        set_pair = (
            self.comparison_set_id is not None,
            self.comparison_set_sha256 is not None,
        )
        if set_pair[0] != set_pair[1]:
            raise ValueError('comparison set id/hash must be supplied together')
        viewpoint_ids = [item.viewpoint_id for item in self.viewpoints]
        if len(viewpoint_ids) != len(set(viewpoint_ids)):
            raise ValueError('presentation viewpoints must be unique')
        names = [item.name for item in self.viewpoints]
        if len(names) != len(set(names)):
            raise ValueError('presentation viewpoint names must be unique')
        known = set(viewpoint_ids)
        for section in self.sections:
            if section.viewpoint_id not in known:
                raise ValueError(
                    'section references a viewpoint outside this session'
                )
        section_ids = [item.section_id for item in self.sections]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError('presentation sections must be unique')
        for annotation in self.annotations:
            if (
                annotation.viewpoint_id is not None
                and annotation.viewpoint_id not in known
            ):
                raise ValueError(
                    'annotation references a viewpoint outside this session'
                )
        keys = [(item.kind, item.ref_id) for item in self.evidence_refs]
        if len(keys) != len(set(keys)):
            raise ValueError('session evidence refs must be unique per kind/ref')
        if self.session_sha256 != _hash(self.semantic_payload()):
            raise ValueError('PresentationSession hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'session_id': self.session_id,
            'document_id': self.document_id,
            'label': self.label,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'system_variant_id': self.system_variant_id,
            'system_variant_sha256': self.system_variant_sha256,
            'comparison_set_id': self.comparison_set_id,
            'comparison_set_sha256': self.comparison_set_sha256,
            'status_label': self.status_label,
            'viewpoints': [
                item.model_dump(mode='json') for item in self.viewpoints
            ],
            'sections': [
                item.model_dump(mode='json') for item in self.sections
            ],
            'annotations': [
                item.model_dump(mode='json') for item in self.annotations
            ],
            'evidence_refs': [
                item.model_dump(mode='json') for item in self.evidence_refs
            ],
            'render': self.render.model_dump(mode='json'),
            'author': self.author,
            'created_at_utc': self.created_at_utc,
        }

    def viewpoint(self, viewpoint_id: str) -> PresentationViewpoint | None:
        for item in self.viewpoints:
            if item.viewpoint_id == viewpoint_id:
                return item
        return None

    def ordered_viewpoints(self) -> tuple[PresentationViewpoint, ...]:
        """Replay order: sections first, then unreferenced viewpoints."""
        ordered: list[PresentationViewpoint] = []
        seen: set[str] = set()
        lookup = {item.viewpoint_id: item for item in self.viewpoints}
        for section in self.sections:
            if section.viewpoint_id in seen:
                continue
            seen.add(section.viewpoint_id)
            ordered.append(lookup[section.viewpoint_id])
        for item in self.viewpoints:
            if item.viewpoint_id not in seen:
                ordered.append(item)
        return tuple(ordered)


class PresentationProposal(BaseModel):
    """A client/proposer ask recorded against a session — never a mutation.

    ``variant_candidate`` pins an existing ``SystemVariant`` (a tracked
    engineering proposal); ``annotation_only`` records a presentation-only
    request (label text, visibility ask, camera ask) that deliberately
    carries no engineering effect.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = PRESENTATION_SESSION_SCHEMA_VERSION
    authority_version: Literal['presentation-proposal-1'] = (
        PRESENTATION_PROPOSAL_AUTHORITY_VERSION
    )
    proposal_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    session_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1)
    kind: ProposalKind
    title: str = Field(min_length=1)
    note: str | None = None
    system_variant_id: str | None = Field(default=None, min_length=1)
    system_variant_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    author_label: str | None = Field(default=None, min_length=1)
    created_at_utc: str = Field(min_length=1)
    proposal_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_proposal(self) -> 'PresentationProposal':
        variant_pair = (
            self.system_variant_id is not None,
            self.system_variant_sha256 is not None,
        )
        if variant_pair[0] != variant_pair[1]:
            raise ValueError('system variant id/hash must be supplied together')
        if self.kind == 'variant_candidate' and self.system_variant_id is None:
            raise ValueError('variant_candidate proposal must pin a SystemVariant')
        if self.kind == 'annotation_only' and self.system_variant_id is not None:
            raise ValueError(
                'annotation_only proposal must not claim a SystemVariant'
            )
        if self.proposal_sha256 != _hash(self.semantic_payload()):
            raise ValueError('PresentationProposal hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'proposal_id': self.proposal_id,
            'session_id': self.session_id,
            'session_sha256': self.session_sha256,
            'document_id': self.document_id,
            'kind': self.kind,
            'title': self.title,
            'note': self.note,
            'system_variant_id': self.system_variant_id,
            'system_variant_sha256': self.system_variant_sha256,
            'author_label': self.author_label,
            'created_at_utc': self.created_at_utc,
        }


class SynchronizedSide(BaseModel):
    """One side of an A/B binding — the exact authority it replays."""

    model_config = ConfigDict(frozen=True)

    kind: SyncSideKind
    #: Set for kind='presentation_session'.
    session_id: str | None = Field(default=None, min_length=1)
    session_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    #: Set for kind='comparison_alternative'.
    comparison_set_id: str | None = Field(default=None, min_length=1)
    comparison_set_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    alternative_id: str | None = Field(default=None, min_length=1)
    alternative_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    #: The scene this side renders — recorded so the binding declares
    #: what it pins even before either side is resolved.
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    label: str | None = None

    @model_validator(mode='after')
    def valid_side(self) -> 'SynchronizedSide':
        if self.kind == 'presentation_session':
            if self.session_id is None or self.session_sha256 is None:
                raise ValueError('session side must pin session id+hash')
            if any(
                ref is not None
                for ref in (
                    self.comparison_set_id,
                    self.comparison_set_sha256,
                    self.alternative_id,
                    self.alternative_sha256,
                )
            ):
                raise ValueError('session side must not pin alternative refs')
        else:
            if any(
                ref is None
                for ref in (
                    self.comparison_set_id,
                    self.comparison_set_sha256,
                    self.alternative_id,
                    self.alternative_sha256,
                )
            ):
                raise ValueError('alternative side must pin set+alternative')
            if self.session_id is not None or self.session_sha256 is not None:
                raise ValueError('alternative side must not pin a session')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'kind': self.kind,
            'session_id': self.session_id,
            'session_sha256': self.session_sha256,
            'comparison_set_id': self.comparison_set_id,
            'comparison_set_sha256': self.comparison_set_sha256,
            'alternative_id': self.alternative_id,
            'alternative_sha256': self.alternative_sha256,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'label': self.label,
        }


class SynchronizedReviewBinding(BaseModel):
    """Two authorities declared for lockstep review — never merged state."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = PRESENTATION_SESSION_SCHEMA_VERSION
    authority_version: Literal['presentation-sync-binding-1'] = (
        PRESENTATION_BINDING_AUTHORITY_VERSION
    )
    binding_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    left: SynchronizedSide
    right: SynchronizedSide
    #: When true the review UI applies one camera state to both sides so
    #: the two authorities are replayed in spatial lockstep.
    lockstep_viewpoint: bool = True
    created_at_utc: str = Field(min_length=1)
    binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_binding(self) -> 'SynchronizedReviewBinding':
        if self.left == self.right:
            raise ValueError('A/B binding requires two distinct sides')
        if self.binding_sha256 != _hash(self.semantic_payload()):
            raise ValueError('SynchronizedReviewBinding hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'binding_id': self.binding_id,
            'document_id': self.document_id,
            'label': self.label,
            'left': self.left.model_dump(mode='json'),
            'right': self.right.model_dump(mode='json'),
            'lockstep_viewpoint': self.lockstep_viewpoint,
            'created_at_utc': self.created_at_utc,
        }


SyncStepViewState = Literal['pinned', 'fit', 'unavailable']


class SynchronizedStep(BaseModel):
    """One paired replay step — deterministic output of a binding.

    Each side's view is either that side's pinned viewpoint ('pinned') or
    a deterministic non-pinned fit camera ('fit'); a side whose authority
    does not resolve is 'unavailable' and never silently substituted.
    """

    model_config = ConfigDict(frozen=True)

    index: int = Field(ge=0)
    left_view: SyncStepViewState
    right_view: SyncStepViewState
    left_camera: RoomCameraState | None = None
    right_camera: RoomCameraState | None = None
    left_viewpoint_name: str | None = None
    right_viewpoint_name: str | None = None


def build_viewpoint(
    *,
    name: str,
    camera: RoomCameraState,
    section: SectionPlaneState | None = None,
    hidden_ids: tuple[str, ...] | None = None,
    focus_entity_id: str | None = None,
    overlay: str = 'none',
    note: str | None = None,
    viewpoint_id: str | None = None,
) -> PresentationViewpoint:
    payload: dict[str, Any] = {
        'viewpoint_id': viewpoint_id or str(uuid4()),
        'name': name,
        'camera': camera,
        'section': section,
        'hidden_ids': hidden_ids,
        'focus_entity_id': focus_entity_id,
        'overlay': overlay,
        'note': note,
    }
    provisional = PresentationViewpoint.model_construct(
        **payload, viewpoint_sha256='0' * 64
    )
    return PresentationViewpoint(
        **payload,
        viewpoint_sha256=_hash(provisional.semantic_payload()),
    )


def build_presentation_session(
    *,
    document_id: str,
    label: str,
    scene_revision_id: str,
    scene_content_hash: str,
    viewpoints: tuple[PresentationViewpoint, ...],
    system_variant_id: str | None = None,
    system_variant_sha256: str | None = None,
    comparison_set_id: str | None = None,
    comparison_set_sha256: str | None = None,
    status_label: PresentationStatusLabel = 'draft',
    sections: tuple[PresentationSection, ...] = (),
    annotations: tuple[PresentationAnnotation, ...] = (),
    evidence_refs: tuple[ComparisonEvidenceRef, ...] = (),
    render: PresentationRenderSettings | None = None,
    author: str | None = None,
    created_at_utc: str,
    session_id: str | None = None,
) -> PresentationSession:
    payload: dict[str, Any] = {
        'session_id': session_id or str(uuid4()),
        'document_id': document_id,
        'label': label,
        'scene_revision_id': scene_revision_id,
        'scene_content_hash': scene_content_hash,
        'system_variant_id': system_variant_id,
        'system_variant_sha256': system_variant_sha256,
        'comparison_set_id': comparison_set_id,
        'comparison_set_sha256': comparison_set_sha256,
        'status_label': status_label,
        'viewpoints': tuple(viewpoints),
        'sections': tuple(sections),
        'annotations': tuple(annotations),
        'evidence_refs': tuple(evidence_refs),
        'render': render or PresentationRenderSettings(),
        'author': author,
        'created_at_utc': created_at_utc,
    }
    provisional = PresentationSession.model_construct(
        **payload, session_sha256='0' * 64
    )
    return PresentationSession(
        **payload,
        session_sha256=_hash(provisional.semantic_payload()),
    )


def build_presentation_proposal(
    *,
    session: PresentationSession,
    kind: ProposalKind,
    title: str,
    note: str | None = None,
    system_variant_id: str | None = None,
    system_variant_sha256: str | None = None,
    author_label: str | None = None,
    created_at_utc: str,
    proposal_id: str | None = None,
) -> PresentationProposal:
    payload: dict[str, Any] = {
        'proposal_id': proposal_id or str(uuid4()),
        'session_id': session.session_id,
        'session_sha256': session.session_sha256,
        'document_id': session.document_id,
        'kind': kind,
        'title': title,
        'note': note,
        'system_variant_id': system_variant_id,
        'system_variant_sha256': system_variant_sha256,
        'author_label': author_label,
        'created_at_utc': created_at_utc,
    }
    provisional = PresentationProposal.model_construct(
        **payload, proposal_sha256='0' * 64
    )
    return PresentationProposal(
        **payload,
        proposal_sha256=_hash(provisional.semantic_payload()),
    )


def build_sync_binding(
    *,
    document_id: str,
    label: str,
    left: SynchronizedSide,
    right: SynchronizedSide,
    lockstep_viewpoint: bool = True,
    created_at_utc: str,
    binding_id: str | None = None,
) -> SynchronizedReviewBinding:
    payload: dict[str, Any] = {
        'binding_id': binding_id or str(uuid4()),
        'document_id': document_id,
        'label': label,
        'left': left,
        'right': right,
        'lockstep_viewpoint': lockstep_viewpoint,
        'created_at_utc': created_at_utc,
    }
    provisional = SynchronizedReviewBinding.model_construct(
        **payload, binding_sha256='0' * 64
    )
    return SynchronizedReviewBinding(
        **payload,
        binding_sha256=_hash(provisional.semantic_payload()),
    )


def synchronized_steps(
    left_viewpoints: tuple[PresentationViewpoint, ...],
    right_viewpoints: tuple[PresentationViewpoint, ...],
) -> tuple[SynchronizedStep, ...]:
    """Pair two viewpoint sequences deterministically for lockstep replay.

    Pairing is by ordinal position — never by name matching, which would
    let a renamed viewpoint change replay semantics. A shorter side
    contributes no viewpoint at that step; the UI falls back to a
    deterministic fit camera recorded as 'fit', or 'unavailable' when the
    side itself could not be materialized.
    """
    count = max(len(left_viewpoints), len(right_viewpoints), 1)
    steps: list[SynchronizedStep] = []
    for index in range(count):
        left = left_viewpoints[index] if index < len(left_viewpoints) else None
        right = right_viewpoints[index] if index < len(right_viewpoints) else None
        steps.append(
            SynchronizedStep(
                index=index,
                left_view='pinned' if left is not None else 'fit',
                right_view='pinned' if right is not None else 'fit',
                left_camera=None if left is None else left.camera,
                right_camera=None if right is None else right.camera,
                left_viewpoint_name=None if left is None else left.name,
                right_viewpoint_name=None if right is None else right.name,
            )
        )
    return tuple(steps)


__all__ = [
    'PRESENTATION_SESSION_SCHEMA_VERSION',
    'PRESENTATION_SESSION_AUTHORITY_VERSION',
    'PRESENTATION_BINDING_AUTHORITY_VERSION',
    'PRESENTATION_PROPOSAL_AUTHORITY_VERSION',
    'PresentationAnnotation',
    'PresentationOverlay',
    'PresentationProposal',
    'PresentationRenderSettings',
    'PresentationSection',
    'PresentationSession',
    'PresentationStatusLabel',
    'PresentationViewpoint',
    'ProposalKind',
    'SyncSideKind',
    'SyncStepViewState',
    'SynchronizedReviewBinding',
    'SynchronizedSide',
    'SynchronizedStep',
    'build_presentation_proposal',
    'build_presentation_session',
    'build_sync_binding',
    'build_viewpoint',
    'synchronized_steps',
]
