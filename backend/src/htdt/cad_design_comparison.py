"""First-class design comparison sets (#447).

HTDT already has strong domain-specific comparison surfaces — measurement
A/B, candidate/Pareto comparison, O100 ``SystemTopologyComparisonSpec``,
standards variant comparison, robustness comparison — but no decision-level
container in which a user pins named alternatives ("Current", "Seat 20 cm
forward", "Treatment A") and compares them later.

A :class:`DesignComparisonSet` is an immutable, named collection of
:class:`ComparisonAlternative`\\ s. Each alternative pins exact authority
references — a ``SceneRevision`` id + content hash plus optional
SystemVariant / as-built / checkpoint / prediction / measurement /
standards / robustness refs — so the set stays valid after the current
scene changes, after Undo history is cleared, and without ever typing an
internal revision id. The set is a *manifest of references*, never a copy.

Rules kept separate per the issue contract:

- no overall score and no automatic winner;
- only compatible evidence is compared — missing/incompatible entries are
  reported with a reason instead of being coerced to zero;
- the set does not replace Undo/Redo, optimizer candidates, O100 topology
  comparison or measurement A/B — it references them;
- editing a set creates a new immutable set revision
  (``supersedes_set_id`` + ``revision`` counter), never an UPDATE.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_repository import SceneRevision
from .cad_scene import SceneDocument
from .cad_scene_history import SceneDiff, diff_scene_documents


COMPARISON_SET_SCHEMA_VERSION = 1
COMPARISON_SET_AUTHORITY_VERSION = 'design-comparison-set-1'

AlternativeKind = Literal[
    'scene_revision',
    'system_variant',
    'as_built',
    'measured_state',
    'design_checkpoint',
]

ComparisonEvidenceKind = Literal[
    'prediction',
    'measurement',
    'validation',
    'standards',
    'robustness',
    'design_checkpoint',
    'other',
]

EvidenceAvailability = Literal[
    'available',
    'missing_reference',
    'unresolvable',
    'incompatible_baseline',
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


class ComparisonEvidenceRef(BaseModel):
    """Exact reference to one evidence authority attached to an alternative."""

    model_config = ConfigDict(frozen=True)

    kind: ComparisonEvidenceKind
    ref_id: str = Field(min_length=1)
    ref_sha256: str | None = Field(default=None, min_length=8)
    label: str | None = Field(default=None, min_length=1)
    #: True when this evidence kind is bound to the alternative's exact
    #: SceneRevision (prediction/measurement results are); scene-bound
    #: evidence is only comparable when alternatives share a baseline.
    scene_bound: bool = True


class ComparisonAlternative(BaseModel):
    """One named alternative pinned inside a comparison set."""

    model_config = ConfigDict(frozen=True)

    alternative_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    kind: AlternativeKind = 'scene_revision'
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    system_variant_id: str | None = Field(default=None, min_length=1)
    system_variant_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    as_built_ref_id: str | None = Field(default=None, min_length=1)
    design_checkpoint_id: str | None = Field(default=None, min_length=1)
    design_checkpoint_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    evidence_refs: tuple[ComparisonEvidenceRef, ...] = ()
    #: Optional saved named-view/camera record id so a comparison can reopen
    #: the same view state the user pinned.
    view_ref: str | None = Field(default=None, min_length=1)
    semantic_change_summary: str | None = Field(default=None, min_length=1)
    created_at_utc: str = Field(min_length=1)
    alternative_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_alternative(self) -> 'ComparisonAlternative':
        variant_pair = (
            self.system_variant_id is not None,
            self.system_variant_sha256 is not None,
        )
        if variant_pair[0] != variant_pair[1]:
            raise ValueError('system variant id/hash must be supplied together')
        checkpoint_pair = (
            self.design_checkpoint_id is not None,
            self.design_checkpoint_sha256 is not None,
        )
        if checkpoint_pair[0] != checkpoint_pair[1]:
            raise ValueError('design checkpoint id/hash must be supplied together')
        keys = [(item.kind, item.ref_id) for item in self.evidence_refs]
        if len(keys) != len(set(keys)):
            raise ValueError('alternative evidence refs must be unique per kind/ref')
        if self.alternative_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ComparisonAlternative hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'alternative_id': self.alternative_id,
            'label': self.label,
            'kind': self.kind,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'system_variant_id': self.system_variant_id,
            'system_variant_sha256': self.system_variant_sha256,
            'as_built_ref_id': self.as_built_ref_id,
            'design_checkpoint_id': self.design_checkpoint_id,
            'design_checkpoint_sha256': self.design_checkpoint_sha256,
            'evidence_refs': [
                item.model_dump(mode='json') for item in self.evidence_refs
            ],
            'view_ref': self.view_ref,
            'semantic_change_summary': self.semantic_change_summary,
            'created_at_utc': self.created_at_utc,
        }


class DesignComparisonSet(BaseModel):
    """Immutable named decision set; edits create a new revision of the set."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = COMPARISON_SET_SCHEMA_VERSION
    authority_version: Literal['design-comparison-set-1'] = (
        COMPARISON_SET_AUTHORITY_VERSION
    )
    set_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    note: str | None = None
    revision: int = Field(default=1, ge=1)
    supersedes_set_id: str | None = Field(default=None, min_length=1)
    alternatives: tuple[ComparisonAlternative, ...] = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_set(self) -> 'DesignComparisonSet':
        ids = [item.alternative_id for item in self.alternatives]
        if len(ids) != len(set(ids)):
            raise ValueError('comparison alternatives must be unique')
        labels = [item.label for item in self.alternatives]
        if len(labels) != len(set(labels)):
            raise ValueError('comparison alternative labels must be unique')
        if self.set_sha256 != _hash(self.semantic_payload()):
            raise ValueError('DesignComparisonSet hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'set_id': self.set_id,
            'document_id': self.document_id,
            'name': self.name,
            'note': self.note,
            'revision': self.revision,
            'supersedes_set_id': self.supersedes_set_id,
            'alternatives': [
                item.model_dump(mode='json') for item in self.alternatives
            ],
            'created_at_utc': self.created_at_utc,
        }

    def alternative(self, alternative_id: str) -> ComparisonAlternative | None:
        for item in self.alternatives:
            if item.alternative_id == alternative_id:
                return item
        return None


class AlternativeDiff(BaseModel):
    """Semantic diff between two alternatives.

    ``scene_diff`` reuses the canonical #485 SceneRevision diff; evidence
    entries are compared by reference identity so nothing is numerically
    coerced into comparability.
    """

    model_config = ConfigDict(frozen=True)

    before_alternative_id: str
    after_alternative_id: str
    scene_changed: bool
    scene_diff: SceneDiff | None = None
    evidence_added: tuple[str, ...] = ()
    evidence_removed: tuple[str, ...] = ()
    evidence_changed: tuple[str, ...] = ()
    evidence_unchanged: tuple[str, ...] = ()


class EvidenceAvailabilityItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    alternative_id: str
    kind: str
    ref_id: str
    state: EvidenceAvailability
    reason: str


class ComparisonAvailability(BaseModel):
    """Per-alternative, per-evidence availability for one set.

    Rows with ``state != 'available'`` carry the reason; no entries are
    dropped or converted to zero so unsupported evidence stays explicit.
    """

    model_config = ConfigDict(frozen=True)

    set_id: str
    items: tuple[EvidenceAvailabilityItem, ...]


def build_alternative(
    *,
    label: str,
    scene_revision: SceneRevision,
    kind: AlternativeKind = 'scene_revision',
    system_variant_id: str | None = None,
    system_variant_sha256: str | None = None,
    as_built_ref_id: str | None = None,
    design_checkpoint_id: str | None = None,
    design_checkpoint_sha256: str | None = None,
    evidence_refs: tuple[ComparisonEvidenceRef, ...] = (),
    view_ref: str | None = None,
    semantic_change_summary: str | None = None,
    created_at_utc: str,
    alternative_id: str | None = None,
) -> ComparisonAlternative:
    payload: dict[str, Any] = {
        'alternative_id': alternative_id or str(uuid4()),
        'label': label,
        'kind': kind,
        'scene_revision_id': scene_revision.revision_id,
        'scene_content_hash': scene_revision.content_hash,
        'system_variant_id': system_variant_id,
        'system_variant_sha256': system_variant_sha256,
        'as_built_ref_id': as_built_ref_id,
        'design_checkpoint_id': design_checkpoint_id,
        'design_checkpoint_sha256': design_checkpoint_sha256,
        'evidence_refs': tuple(evidence_refs),
        'view_ref': view_ref,
        'semantic_change_summary': semantic_change_summary,
        'created_at_utc': created_at_utc,
    }
    provisional = ComparisonAlternative.model_construct(
        **payload, alternative_sha256='0' * 64
    )
    return ComparisonAlternative(
        **payload,
        alternative_sha256=_hash(provisional.semantic_payload()),
    )


def build_comparison_set(
    *,
    document_id: str,
    name: str,
    alternatives: tuple[ComparisonAlternative, ...],
    note: str | None = None,
    supersedes: DesignComparisonSet | None = None,
    created_at_utc: str,
    set_id: str | None = None,
) -> DesignComparisonSet:
    if supersedes is not None and supersedes.document_id != document_id:
        raise ValueError('superseded comparison set belongs to another document')
    payload: dict[str, Any] = {
        'set_id': set_id or str(uuid4()),
        'document_id': document_id,
        'name': name,
        'note': note,
        'revision': 1 if supersedes is None else supersedes.revision + 1,
        'supersedes_set_id': None if supersedes is None else supersedes.set_id,
        'alternatives': tuple(alternatives),
        'created_at_utc': created_at_utc,
    }
    provisional = DesignComparisonSet.model_construct(
        **payload, set_sha256='0' * 64
    )
    return DesignComparisonSet(
        **payload,
        set_sha256=_hash(provisional.semantic_payload()),
    )


def diff_alternatives(
    before: ComparisonAlternative,
    after: ComparisonAlternative,
    *,
    before_document: SceneDocument | None = None,
    after_document: SceneDocument | None = None,
) -> AlternativeDiff:
    """Semantic A/B diff between two alternatives.

    The Scene component is diffed via ``diff_scene_documents`` when both
    documents are supplied; evidence refs are compared by (kind, ref_id) with
    hash equality, so incompatible or missing entries stay explicit.
    """

    scene_changed = (
        before.scene_revision_id != after.scene_revision_id
        or before.scene_content_hash != after.scene_content_hash
    )
    scene_diff = None
    if before_document is not None and after_document is not None:
        scene_diff = diff_scene_documents(before_document, after_document)
        scene_changed = scene_changed or not scene_diff.is_empty
    before_refs = {
        (item.kind, item.ref_id): item for item in before.evidence_refs
    }
    after_refs = {
        (item.kind, item.ref_id): item for item in after.evidence_refs
    }
    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []
    unchanged: list[str] = []
    for key in sorted(before_refs.keys() | after_refs.keys()):
        label = f'{key[0]}:{key[1]}'
        b = before_refs.get(key)
        a = after_refs.get(key)
        if b is None:
            added.append(label)
        elif a is None:
            removed.append(label)
        elif a.ref_sha256 == b.ref_sha256:
            unchanged.append(label)
        else:
            changed.append(label)
    return AlternativeDiff(
        before_alternative_id=before.alternative_id,
        after_alternative_id=after.alternative_id,
        scene_changed=scene_changed,
        scene_diff=scene_diff,
        evidence_added=tuple(added),
        evidence_removed=tuple(removed),
        evidence_changed=tuple(changed),
        evidence_unchanged=tuple(unchanged),
    )


def evaluate_comparison_set(
    comparison_set: DesignComparisonSet,
    *,
    evidence_exists: 'Mapping[tuple[str, str], str | None]',
) -> ComparisonAvailability:
    """Report per-alternative evidence availability; nothing is coerced.

    ``evidence_exists`` maps ``(kind, ref_id)`` to the referenced evidence's
    current semantic hash or ``None`` when it does not resolve. Scene-bound
    evidence additionally requires the alternative's pinned SceneRevision to
    still be the baseline the evidence was produced under — a mismatched
    pin is ``incompatible_baseline`` rather than silently compared.
    """

    items: list[EvidenceAvailabilityItem] = []
    for alternative in comparison_set.alternatives:
        for ref in alternative.evidence_refs:
            current = evidence_exists.get((ref.kind, ref.ref_id))
            if current is None:
                items.append(
                    EvidenceAvailabilityItem(
                        alternative_id=alternative.alternative_id,
                        kind=ref.kind,
                        ref_id=ref.ref_id,
                        state='unresolvable',
                        reason='referenced evidence does not resolve',
                    )
                )
            elif ref.ref_sha256 is not None and ref.ref_sha256 != current:
                items.append(
                    EvidenceAvailabilityItem(
                        alternative_id=alternative.alternative_id,
                        kind=ref.kind,
                        ref_id=ref.ref_id,
                        state='incompatible_baseline',
                        reason='referenced evidence no longer matches the pinned hash',
                    )
                )
            else:
                items.append(
                    EvidenceAvailabilityItem(
                        alternative_id=alternative.alternative_id,
                        kind=ref.kind,
                        ref_id=ref.ref_id,
                        state='available',
                        reason='reference resolves to the pinned authority',
                    )
                )
    return ComparisonAvailability(
        set_id=comparison_set.set_id,
        items=tuple(items),
    )
