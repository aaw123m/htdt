"""Project design checkpoints (#619).

``SceneRevision`` history is the canonical authority for Scene content, but
the mutable constraint workspace, the selected system variant and other
project-level design authorities evolve outside it. "Restore this saved
Scene" is therefore not the same operation as "restore this whole design
state". A :class:`ProjectDesignCheckpoint` is an immutable manifest that pins
the exact set of authorities a user accepted as one design decision point:

- the exact ``SceneRevision`` (id + content hash);
- an immutable :class:`ConstraintWorkspaceSnapshot` of the otherwise mutable
  ``CadConstraintSet`` sidecar;
- optional exact references to the other versioned design authorities that
  existed on main at the time (system topology, selected ``SystemVariant``,
  layout profile, target curve, standards profile, ...).

The checkpoint never resolves a mutable sidecar by "current/latest" at read
time: every included component is pinned by an immutable identity or a
snapshot hash. Restoring a checkpoint always creates *new* current authority
(a new head ``SceneRevision`` descending from the live head, and a new
constraint-workspace generation written through the mutable repository), so
history is never rewritten and restored design state does not touch
as-built/measured physical evidence.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_constraint_models import CadConstraintSet
from .cad_constraint_repository import CadConstraintRepository
from .cad_repository import SceneRepository, SceneRevision
from .cad_scene import SceneDocument


CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_AUTHORITY_VERSION = 'project-design-checkpoint-1'
CONSTRAINT_SNAPSHOT_AUTHORITY_VERSION = 'constraint-workspace-snapshot-1'

CheckpointComponentKind = Literal[
    'scene_revision',
    'constraint_workspace',
    'system_topology',
    'system_variant',
    'layout_profile',
    'target_curve',
    'standards_profile',
    'room_operating_state',
    'operating_preset',
    'other',
]
CHECKPOINT_COMPONENT_KINDS: frozenset[str] = frozenset(
    {
        'scene_revision',
        'constraint_workspace',
        'system_topology',
        'system_variant',
        'layout_profile',
        'target_curve',
        'standards_profile',
        'room_operating_state',
        'operating_preset',
        'other',
    }
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


def constraint_workspace_sha256(constraint_set: CadConstraintSet) -> str:
    """Semantic identity of one constraint-workspace payload.

    SearchSpec already reasons about a constraint-workspace semantic identity;
    this digest reuses the canonical JSON of the ``CadConstraintSet`` payload
    rather than inventing a second constraint language.
    """

    return _hash(constraint_set.model_dump(mode='json'))


class CheckpointAuthorityRef(BaseModel):
    """Exact reference to one versioned design authority pinned by a checkpoint.

    ``ref_sha256`` is required whenever the referenced authority carries a
    semantic hash; authorities without one (plain revision ids) may omit it,
    in which case ``ref_id`` alone is the pin.
    """

    model_config = ConfigDict(frozen=True)

    kind: CheckpointComponentKind
    ref_id: str = Field(min_length=1)
    ref_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    label: str | None = Field(default=None, min_length=1)


class ConstraintWorkspaceSnapshot(BaseModel):
    """Immutable snapshot of the mutable ``CadConstraintSet`` workspace.

    Captures the exact payload plus the mutable workspace's ``updated_at_utc``
    generation marker so later edits never change what a checkpoint saw, and
    a checkpoint can name the generation it was taken from.
    """

    model_config = ConfigDict(frozen=True)

    snapshot_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    constraint_set: CadConstraintSet
    constraint_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_updated_at_utc: str | None = None
    created_at_utc: str = Field(min_length=1)
    snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_snapshot(self) -> 'ConstraintWorkspaceSnapshot':
        if self.constraint_set.document_id != self.document_id:
            raise ValueError('constraint snapshot document mismatch')
        if self.constraint_sha256 != constraint_workspace_sha256(self.constraint_set):
            raise ValueError('constraint snapshot payload hash mismatch')
        if self.snapshot_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ConstraintWorkspaceSnapshot hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': CONSTRAINT_SNAPSHOT_AUTHORITY_VERSION,
            'snapshot_id': self.snapshot_id,
            'document_id': self.document_id,
            'constraint_sha256': self.constraint_sha256,
            'source_updated_at_utc': self.source_updated_at_utc,
            'created_at_utc': self.created_at_utc,
        }


class ProjectDesignCheckpoint(BaseModel):
    """Immutable manifest binding one accepted design decision point.

    A manifest of references, not a copy of every authority. The Scene half
    is always pinned by exact ``SceneRevision`` id + content hash; every other
    component is pinned through ``component_refs`` or the constraint snapshot.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = CHECKPOINT_SCHEMA_VERSION
    authority_version: Literal['project-design-checkpoint-1'] = (
        CHECKPOINT_AUTHORITY_VERSION
    )
    checkpoint_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    note: str | None = None
    created_at_utc: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    constraint_snapshot_id: str | None = Field(default=None, min_length=1)
    constraint_snapshot_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    component_refs: tuple[CheckpointAuthorityRef, ...] = ()
    checkpoint_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_checkpoint(self) -> 'ProjectDesignCheckpoint':
        snapshot_pair = (
            self.constraint_snapshot_id is not None,
            self.constraint_snapshot_sha256 is not None,
        )
        if snapshot_pair[0] != snapshot_pair[1]:
            raise ValueError('constraint snapshot id/hash must be supplied together')
        keys = [(item.kind, item.ref_id) for item in self.component_refs]
        if len(keys) != len(set(keys)):
            raise ValueError('checkpoint component refs must be unique per kind/ref')
        if any(item.kind == 'scene_revision' for item in self.component_refs):
            raise ValueError('the Scene component is pinned by dedicated fields only')
        if any(item.kind == 'constraint_workspace' for item in self.component_refs):
            raise ValueError('the constraint workspace is pinned by dedicated fields only')
        if self.checkpoint_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ProjectDesignCheckpoint hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'checkpoint_id': self.checkpoint_id,
            'document_id': self.document_id,
            'title': self.title,
            'note': self.note,
            'created_at_utc': self.created_at_utc,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'constraint_snapshot_id': self.constraint_snapshot_id,
            'constraint_snapshot_sha256': self.constraint_snapshot_sha256,
            'component_refs': [
                item.model_dump(mode='json') for item in self.component_refs
            ],
        }


class CheckpointRestoreRecord(BaseModel):
    """Append-only fact: one checkpoint was applied to produce new current state."""

    model_config = ConfigDict(frozen=True)

    restore_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    checkpoint_id: str = Field(min_length=1)
    checkpoint_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    applied_components: tuple[CheckpointComponentKind, ...]
    new_scene_revision_id: str | None = Field(default=None, min_length=1)
    new_constraint_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    created_at_utc: str = Field(min_length=1)
    restore_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_restore(self) -> 'CheckpointRestoreRecord':
        if len(self.applied_components) != len(set(self.applied_components)):
            raise ValueError('restore applied components must be unique')
        if 'scene_revision' in self.applied_components and not self.new_scene_revision_id:
            raise ValueError('scene restore requires the produced revision id')
        if 'constraint_workspace' in self.applied_components and not self.new_constraint_sha256:
            raise ValueError('constraint restore requires the produced workspace hash')
        if self.restore_sha256 != _hash(self.semantic_payload()):
            raise ValueError('CheckpointRestoreRecord hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'restore_id': self.restore_id,
            'document_id': self.document_id,
            'checkpoint_id': self.checkpoint_id,
            'checkpoint_sha256': self.checkpoint_sha256,
            'applied_components': list(self.applied_components),
            'new_scene_revision_id': self.new_scene_revision_id,
            'new_constraint_sha256': self.new_constraint_sha256,
            'created_at_utc': self.created_at_utc,
        }


class CheckpointComponentDiff(BaseModel):
    """One compared component inside a checkpoint-to-checkpoint diff."""

    model_config = ConfigDict(frozen=True)

    kind: str
    state: Literal['unchanged', 'changed', 'added', 'removed']
    before_ref: str | None = None
    after_ref: str | None = None


class CheckpointDiff(BaseModel):
    """Per-component comparison of two checkpoints; never a single score."""

    model_config = ConfigDict(frozen=True)

    before_checkpoint_id: str
    after_checkpoint_id: str
    components: tuple[CheckpointComponentDiff, ...]


def snapshot_constraint_workspace(
    constraint_set: CadConstraintSet,
    *,
    source_updated_at_utc: str | None = None,
    created_at_utc: str,
    snapshot_id: str | None = None,
) -> ConstraintWorkspaceSnapshot:
    """Freeze the mutable constraint workspace into an immutable snapshot."""

    payload: dict[str, Any] = {
        'snapshot_id': snapshot_id or str(uuid4()),
        'document_id': constraint_set.document_id,
        'constraint_set': constraint_set,
        'constraint_sha256': constraint_workspace_sha256(constraint_set),
        'source_updated_at_utc': source_updated_at_utc,
        'created_at_utc': created_at_utc,
    }
    provisional = ConstraintWorkspaceSnapshot.model_construct(
        **payload, snapshot_sha256='0' * 64
    )
    return ConstraintWorkspaceSnapshot(
        **payload,
        snapshot_sha256=_hash(provisional.semantic_payload()),
    )


def build_design_checkpoint(
    *,
    document_id: str,
    title: str,
    scene_revision: SceneRevision,
    constraint_snapshot: ConstraintWorkspaceSnapshot | None = None,
    component_refs: tuple[CheckpointAuthorityRef, ...] = (),
    note: str | None = None,
    created_at_utc: str,
    checkpoint_id: str | None = None,
) -> ProjectDesignCheckpoint:
    """Bind an exact design-state manifest at one decision point.

    The scene component must be an exact persisted ``SceneRevision`` of this
    document; callers pass committed authority only (checkpointing dirty
    editor state is out of scope — compose with dirty-state resolution before
    calling).
    """

    if scene_revision.document_id != document_id:
        raise ValueError('checkpoint SceneRevision belongs to another document')
    if constraint_snapshot is not None and constraint_snapshot.document_id != document_id:
        raise ValueError('constraint snapshot belongs to another document')
    payload: dict[str, Any] = {
        'checkpoint_id': checkpoint_id or str(uuid4()),
        'document_id': document_id,
        'title': title,
        'note': note,
        'created_at_utc': created_at_utc,
        'scene_revision_id': scene_revision.revision_id,
        'scene_content_hash': scene_revision.content_hash,
        'constraint_snapshot_id': (
            None if constraint_snapshot is None else constraint_snapshot.snapshot_id
        ),
        'constraint_snapshot_sha256': (
            None if constraint_snapshot is None else constraint_snapshot.snapshot_sha256
        ),
        'component_refs': tuple(component_refs),
    }
    provisional = ProjectDesignCheckpoint.model_construct(
        **payload, checkpoint_sha256='0' * 64
    )
    return ProjectDesignCheckpoint(
        **payload,
        checkpoint_sha256=_hash(provisional.semantic_payload()),
    )


def diff_checkpoints(
    before: ProjectDesignCheckpoint,
    after: ProjectDesignCheckpoint,
) -> CheckpointDiff:
    """Compare two manifests per component; unchanged refs stay unchanged."""

    components: list[CheckpointComponentDiff] = [
        CheckpointComponentDiff(
            kind='scene_revision',
            state=(
                'unchanged'
                if before.scene_revision_id == after.scene_revision_id
                and before.scene_content_hash == after.scene_content_hash
                else 'changed'
            ),
            before_ref=before.scene_revision_id,
            after_ref=after.scene_revision_id,
        ),
        CheckpointComponentDiff(
            kind='constraint_workspace',
            state=_snapshot_state(before, after),
            before_ref=before.constraint_snapshot_id,
            after_ref=after.constraint_snapshot_id,
        ),
    ]
    before_refs = {(item.kind, item.ref_id): item for item in before.component_refs}
    after_refs = {(item.kind, item.ref_id): item for item in after.component_refs}
    for key in sorted(before_refs.keys() | after_refs.keys()):
        b = before_refs.get(key)
        a = after_refs.get(key)
        if b is None:
            components.append(
                CheckpointComponentDiff(
                    kind=key[0], state='added', after_ref=a.ref_id
                )
            )
        elif a is None:
            components.append(
                CheckpointComponentDiff(
                    kind=key[0], state='removed', before_ref=b.ref_id
                )
            )
        else:
            components.append(
                CheckpointComponentDiff(
                    kind=key[0],
                    state='unchanged' if b.ref_sha256 == a.ref_sha256 else 'changed',
                    before_ref=b.ref_id,
                    after_ref=a.ref_id,
                )
            )
    return CheckpointDiff(
        before_checkpoint_id=before.checkpoint_id,
        after_checkpoint_id=after.checkpoint_id,
        components=tuple(components),
    )


def _snapshot_state(
    before: ProjectDesignCheckpoint, after: ProjectDesignCheckpoint
) -> Literal['unchanged', 'changed', 'added', 'removed']:
    if before.constraint_snapshot_id is None and after.constraint_snapshot_id is None:
        return 'unchanged'
    if before.constraint_snapshot_id is None:
        return 'added'
    if after.constraint_snapshot_id is None:
        return 'removed'
    return (
        'unchanged'
        if before.constraint_snapshot_sha256 == after.constraint_snapshot_sha256
        else 'changed'
    )


def restore_design_checkpoint(
    checkpoint: ProjectDesignCheckpoint,
    *,
    scene_repository: SceneRepository,
    constraint_repository: CadConstraintRepository,
    snapshot: ConstraintWorkspaceSnapshot | None = None,
    components: tuple[CheckpointComponentKind, ...] | None = None,
    created_at_utc: str,
    restore_id: str | None = None,
) -> CheckpointRestoreRecord:
    """Apply a checkpoint by creating NEW current authority, never rewriting.

    - ``scene_revision``: copies the pinned revision's ``SceneDocument`` into a
      new head ``SceneRevision`` descending from the document's live head;
    - ``constraint_workspace``: writes the pinned snapshot payload back through
      the mutable ``CadConstraintRepository`` as a new workspace generation;
    - every other component kind is reference-only: a partial restore states
      exactly which components are applied via ``applied_components``.

    The pinned ``SceneRevision`` row, the snapshot and all referenced
    evidence remain untouched; as-built/measured records are never rewritten
    by a design-state restore.
    """

    wanted = (
        set(CHECKPOINT_COMPONENT_KINDS) if components is None else set(components)
    )
    source_revision = scene_repository.get(checkpoint.scene_revision_id)
    if source_revision is None:
        raise ValueError('checkpoint SceneRevision is not persisted')
    if source_revision.content_hash != checkpoint.scene_content_hash:
        raise ValueError('checkpoint SceneRevision content hash mismatch')

    applied: list[CheckpointComponentKind] = []
    new_scene_revision_id: str | None = None
    new_constraint_sha256: str | None = None

    if 'scene_revision' in wanted:
        head = scene_repository.current_head(checkpoint.document_id)
        if head is None:
            raise ValueError('document has no current head to restore onto')
        restored_document = SceneDocument.model_validate(
            source_revision.document.model_dump(mode='json')
        )
        result = scene_repository.save(
            restored_document,
            parent_revision_id=head.revision_id,
        )
        new_scene_revision_id = result.revision.revision_id
        applied.append('scene_revision')

    if 'constraint_workspace' in wanted:
        if checkpoint.constraint_snapshot_id is None:
            raise ValueError('checkpoint carries no constraint snapshot')
        if snapshot is None:
            raise ValueError('constraint snapshot must be supplied for restore')
        if snapshot.snapshot_id != checkpoint.constraint_snapshot_id:
            raise ValueError('constraint snapshot id mismatch')
        if snapshot.snapshot_sha256 != checkpoint.constraint_snapshot_sha256:
            raise ValueError('constraint snapshot hash mismatch')
        constraint_repository.save(snapshot.constraint_set)
        new_constraint_sha256 = constraint_workspace_sha256(snapshot.constraint_set)
        applied.append('constraint_workspace')

    reference_only = [
        item.kind
        for item in checkpoint.component_refs
        if item.kind in wanted
        and item.kind not in applied
        and item.kind not in {'scene_revision', 'constraint_workspace'}
    ]
    applied.extend(sorted(set(reference_only)))

    payload: dict[str, Any] = {
        'restore_id': restore_id or str(uuid4()),
        'document_id': checkpoint.document_id,
        'checkpoint_id': checkpoint.checkpoint_id,
        'checkpoint_sha256': checkpoint.checkpoint_sha256,
        'applied_components': tuple(applied),
        'new_scene_revision_id': new_scene_revision_id,
        'new_constraint_sha256': new_constraint_sha256,
        'created_at_utc': created_at_utc,
    }
    provisional = CheckpointRestoreRecord.model_construct(
        **payload, restore_sha256='0' * 64
    )
    return CheckpointRestoreRecord(
        **payload,
        restore_sha256=_hash(provisional.semantic_payload()),
    )
