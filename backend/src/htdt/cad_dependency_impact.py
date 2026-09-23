"""Dependency impact / revalidation read model (#561).

This module is NOT a new source of truth. It summarizes the existing exact
authority bindings (scene revisions, content hashes, entity bindings) into a
structured :class:`DependencyImpactReport` answering: "I changed this one
thing — what is now stale, why, and what must be re-run or re-measured?"

Change axes keep invalidation narrow: a target-curve change does not stale
geometry evidence; a projector change does not stale acoustic results. When
dependency precision is not provable the report fails conservatively and
says so. Historical artifacts bound to an old revision remain valid evidence
for that revision — 'stale' here means "not current", never "invalid".
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_repository import SceneRevision
from .cad_scene import SceneDocument, SceneEntity


IMPACT_SCHEMA_VERSION = 1
IMPACT_AUTHORITY_VERSION = 'dependency-impact-1'

DependencyAxis = Literal[
    'geometry',
    'source_equipment',
    'material_boundary',
    'topology_routing',
    'operating_state',
    'target_design',
    'measurement_context',
    'calibration_settings',
    'solver_provider',
    'unknown',
]

ChangeKind = Literal[
    'entity_added',
    'entity_removed',
    'entity_modified',
    'room_geometry',
    'authority_reference',
]

ImpactAction = Literal[
    'recompute',
    're_evaluate',
    're_import',
    'remeasure',
    're_commission',
    'none',
]

ImpactState = Literal['stale', 'unaffected', 'uncertain']

ArtifactKind = Literal[
    'prediction',
    'coverage_evaluation',
    'direct_level_evaluation',
    'seat_priority_profile',
    'cost_evaluation',
    'measurement_plan',
    'measured_dataset',
    'calibration_plan',
    'installation_report',
    'optimization_run',
    'design_comparison',
    'treatment_plan',
    'commissioning_plan',
    'video_geometry',
]


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


class SceneChange(BaseModel):
    """One exact change between two scene documents, with its dependency axes."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: ChangeKind
    axes: frozenset[DependencyAxis]
    entity_id: str | None = None
    detail: str = Field(min_length=1)


_ENTITY_AXES: dict[str, tuple[DependencyAxis, ...]] = {
    'speaker': ('geometry', 'source_equipment'),
    'seat': ('geometry', 'measurement_context'),
    'measurement_point': ('measurement_context',),
    'screen': ('geometry', 'operating_state'),
    'furniture': ('geometry',),
    'av_equipment': ('operating_state', 'topology_routing'),
}


def _entity_axes(kind: str) -> tuple[DependencyAxis, ...]:
    return _ENTITY_AXES.get(kind, ('unknown',))


def _changed_entity_axes(
    before: SceneEntity,
    after: SceneEntity,
) -> frozenset[DependencyAxis]:
    # Modification axes are field-derived only — an unchanged entity emits no
    # change at all. Kind-specific base axes apply to add/remove diffs.
    axes: set[DependencyAxis] = set()
    if before.kind != after.kind:
        axes.update(_entity_axes(before.kind))
        axes.update(_entity_axes(after.kind))
    if before.size_m != after.size_m:
        axes.add('geometry')
    if before.position != after.position or before.orientation != after.orientation:
        axes.add('geometry')
        if before.kind in ('seat', 'measurement_point'):
            axes.add('measurement_context')
        if before.kind == 'speaker':
            axes.add('source_equipment')
    if before.acoustic_reference_offset_m != after.acoustic_reference_offset_m:
        axes.update(('geometry', 'measurement_context'))
    if before.speaker_role != after.speaker_role:
        axes.update(('source_equipment', 'topology_routing'))
    if before.aim_xyz != after.aim_xyz:
        axes.update(('source_equipment', 'geometry'))
    return frozenset(axes)


def diff_scene_documents(
    before: SceneDocument,
    after: SceneDocument,
) -> tuple[SceneChange, ...]:
    """Exact field-level change list between two committed documents."""
    changes: list[SceneChange] = []
    if before.room != after.room:
        changes.append(
            SceneChange(
                kind='room_geometry',
                axes=frozenset({'geometry', 'material_boundary'}),
                entity_id='room',
                detail='room geometry or boundary material changed',
            )
        )
    old = {entity.entity_id: entity for entity in before.entities}
    new = {entity.entity_id: entity for entity in after.entities}
    for entity_id in sorted(set(old) - set(new)):
        changes.append(
            SceneChange(
                kind='entity_removed',
                axes=frozenset(_entity_axes(old[entity_id].kind)),
                entity_id=entity_id,
                detail=f'{old[entity_id].kind} {entity_id} removed',
            )
        )
    for entity_id in sorted(set(new) - set(old)):
        changes.append(
            SceneChange(
                kind='entity_added',
                axes=frozenset(_entity_axes(new[entity_id].kind)),
                entity_id=entity_id,
                detail=f'{new[entity_id].kind} {entity_id} added',
            )
        )
    for entity_id in sorted(set(old) & set(new)):
        axes = _changed_entity_axes(old[entity_id], new[entity_id])
        if not axes:
            continue
        changes.append(
            SceneChange(
                kind='entity_modified',
                axes=axes,
                entity_id=entity_id,
                detail=f'{old[entity_id].kind} {entity_id} modified',
            )
        )
    return tuple(changes)


class WatchedArtifact(BaseModel):
    """One derived artifact registered by a caller, with its exact watches.

    ``watched_axes`` is the artifact's declared dependency surface;
    ``watched_entity_ids`` narrows it further when the artifact provably
    depends on named entities only.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    artifact_kind: ArtifactKind
    artifact_id: str = Field(min_length=1)
    bound_revision_id: str | None = None
    bound_content_hash: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    watched_axes: frozenset[DependencyAxis] = Field(min_length=1)
    watched_entity_ids: tuple[str, ...] = ()
    display_name: str | None = None


class ArtifactImpact(BaseModel):
    """Stale/unaffected verdict for one artifact with the causal edge."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    artifact_kind: ArtifactKind
    artifact_id: str = Field(min_length=1)
    display_name: str | None = None
    state: ImpactState
    action: ImpactAction
    reason: str = Field(min_length=1)
    dependency_edges: tuple[str, ...] = ()


# (artifact_kind, axis) -> required action. Missing pairs inside a changed
# axis fail conservatively to 're_evaluate'.
_ARTIFACT_ACTIONS: dict[tuple[ArtifactKind, DependencyAxis], ImpactAction] = {
    ('prediction', 'geometry'): 'recompute',
    ('prediction', 'source_equipment'): 'recompute',
    ('prediction', 'material_boundary'): 'recompute',
    ('prediction', 'topology_routing'): 'recompute',
    ('prediction', 'operating_state'): 'recompute',
    ('prediction', 'solver_provider'): 'recompute',
    ('coverage_evaluation', 'geometry'): 'recompute',
    ('coverage_evaluation', 'source_equipment'): 'recompute',
    ('direct_level_evaluation', 'geometry'): 'recompute',
    ('direct_level_evaluation', 'source_equipment'): 'recompute',
    ('seat_priority_profile', 'geometry'): 're_evaluate',
    ('cost_evaluation', 'geometry'): 're_evaluate',
    ('cost_evaluation', 'source_equipment'): 're_evaluate',
    ('measurement_plan', 'geometry'): 'remeasure',
    ('measurement_plan', 'measurement_context'): 'remeasure',
    ('measured_dataset', 'measurement_context'): 'none',
    ('calibration_plan', 'geometry'): 're_commission',
    ('calibration_plan', 'source_equipment'): 're_commission',
    ('calibration_plan', 'topology_routing'): 're_commission',
    ('calibration_plan', 'target_design'): 're_commission',
    ('calibration_plan', 'calibration_settings'): 're_commission',
    ('installation_report', 'geometry'): 'recompute',
    ('installation_report', 'source_equipment'): 'recompute',
    ('installation_report', 'topology_routing'): 'recompute',
    ('optimization_run', 'geometry'): 're_evaluate',
    ('optimization_run', 'source_equipment'): 're_evaluate',
    ('optimization_run', 'material_boundary'): 're_evaluate',
    ('optimization_run', 'target_design'): 're_evaluate',
    ('design_comparison', 'target_design'): 're_evaluate',
    ('design_comparison', 'geometry'): 're_evaluate',
    ('treatment_plan', 'material_boundary'): 're_evaluate',
    ('treatment_plan', 'geometry'): 're_evaluate',
    ('commissioning_plan', 'geometry'): 're_commission',
    ('commissioning_plan', 'topology_routing'): 're_commission',
    ('commissioning_plan', 'operating_state'): 're_commission',
    ('video_geometry', 'geometry'): 'recompute',
    ('video_geometry', 'operating_state'): 'recompute',
}

_ACTION_ORDER: tuple[ImpactAction, ...] = (
    'none',
    're_import',
    're_evaluate',
    'recompute',
    'remeasure',
    're_commission',
)


def _artifact_action(
    artifact: WatchedArtifact,
    changed_axes: frozenset[DependencyAxis],
    edges: tuple[str, ...],
) -> tuple[ImpactState, ImpactAction, str]:
    if 'unknown' in changed_axes:
        return (
            'uncertain',
            're_evaluate',
            'change axis precision is unresolved — conservative revalidation',
        )
    if artifact.bound_revision_id is not None and artifact.watched_axes:
        hit = changed_axes & artifact.watched_axes
        if not hit:
            return (
                'unaffected',
                'none',
                'no dependency axis changed '
                f"(artifact watches {sorted(artifact.watched_axes)})",
            )
        missing_rules = [
            axis
            for axis in hit
            if (artifact.artifact_kind, axis) not in _ARTIFACT_ACTIONS
        ]
        if missing_rules:
            return (
                'uncertain',
                're_evaluate',
                'no exact dependency rule for changed axes '
                f'{sorted(missing_rules)} — conservative revalidation',
            )
        worst = max(
            (
                _ARTIFACT_ACTIONS[(artifact.artifact_kind, axis)]
                for axis in hit
            ),
            key=_ACTION_ORDER.index,
        )
        if worst == 'none':
            return (
                'stale',
                'none',
                'bound to a superseded revision but remains valid historical '
                'evidence; no revalidation required',
            )
        return (
            'stale',
            worst,
            f'depends on {sorted(hit)} via ' + '; '.join(edges),
        )
    return (
        'uncertain',
        're_evaluate',
        'artifact declares no dependency axes — conservative revalidation',
    )


class DependencyImpactReport(BaseModel):
    """Versioned impact summary for one revision-to-revision change."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = IMPACT_SCHEMA_VERSION
    authority_version: Literal[
        'dependency-impact-1'
    ] = IMPACT_AUTHORITY_VERSION

    document_id: str = Field(min_length=1)
    from_revision_id: str = Field(min_length=1)
    from_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    to_revision_id: str = Field(min_length=1)
    to_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    changes: tuple[SceneChange, ...]
    changed_axes: frozenset[DependencyAxis]
    impacts: tuple[ArtifactImpact, ...]
    stale_count: int = Field(ge=0)
    uncertain_count: int = Field(ge=0)
    unaffected_count: int = Field(ge=0)

    report_id: str = Field(min_length=1)
    report_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_report(self) -> 'DependencyImpactReport':
        digest = _digest(self.identity_payload())
        if self.report_sha256 != digest:
            raise ValueError('dependency impact report hash mismatch')
        if self.report_id != _semantic_id('impact-report', digest):
            raise ValueError('dependency impact report ID mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'from_revision_id': self.from_revision_id,
            'from_content_hash': self.from_content_hash,
            'to_revision_id': self.to_revision_id,
            'to_content_hash': self.to_content_hash,
            'changes': [item.model_dump(mode='json') for item in self.changes],
            'changed_axes': sorted(self.changed_axes),
            'impacts': [item.model_dump(mode='json') for item in self.impacts],
        }


def build_dependency_impact_report(
    *,
    from_revision: SceneRevision,
    to_revision: SceneRevision,
    artifacts: Sequence[WatchedArtifact],
    extra_changes: Sequence[SceneChange] = (),
) -> DependencyImpactReport:
    """Summarize what changed and what each watched artifact must redo.

    Artifact watches stay exact: only declared axes/entities stale an
    artifact, and only when its bound revision is not the new head. Nothing
    is deleted and no global validity score is produced.
    """
    if from_revision.document_id != to_revision.document_id:
        raise ValueError('impact report requires revisions of one document')
    if from_revision.revision_id == to_revision.revision_id:
        raise ValueError('impact report requires two distinct revisions')

    changes = tuple(
        diff_scene_documents(
            from_revision.document,
            to_revision.document,
        )
    ) + tuple(extra_changes)
    changed_axes: set[DependencyAxis] = set()
    changed_entity_ids: set[str] = set()
    for change in changes:
        changed_axes.update(change.axes)
        if change.entity_id is not None:
            changed_entity_ids.add(change.entity_id)
    axes = frozenset(changed_axes)

    impacts: list[ArtifactImpact] = []
    for artifact in artifacts:
        if artifact.bound_revision_id == to_revision.revision_id:
            impacts.append(
                ArtifactImpact(
                    artifact_kind=artifact.artifact_kind,
                    artifact_id=artifact.artifact_id,
                    display_name=artifact.display_name,
                    state='unaffected',
                    action='none',
                    reason='already bound to the current revision',
                )
            )
            continue
        if (
            artifact.watched_entity_ids
            and not (
                set(artifact.watched_entity_ids) & changed_entity_ids
            )
        ):
            impacts.append(
                ArtifactImpact(
                    artifact_kind=artifact.artifact_kind,
                    artifact_id=artifact.artifact_id,
                    display_name=artifact.display_name,
                    state='unaffected',
                    action='none',
                    reason=(
                        'changed entities do not intersect the artifact’s '
                        'declared entity watches'
                    ),
                )
            )
            continue
        edges = tuple(
            f'{change.entity_id or change.kind}:{change.detail}'
            for change in changes
            if change.axes & artifact.watched_axes
            or 'unknown' in change.axes
        )
        state, action, reason = _artifact_action(artifact, axes, edges)
        impacts.append(
            ArtifactImpact(
                artifact_kind=artifact.artifact_kind,
                artifact_id=artifact.artifact_id,
                display_name=artifact.display_name,
                state=state,
                action=action,
                reason=reason,
                dependency_edges=edges if state != 'unaffected' else (),
            )
        )

    stale = sum(1 for item in impacts if item.state == 'stale')
    uncertain = sum(1 for item in impacts if item.state == 'uncertain')
    unaffected = sum(1 for item in impacts if item.state == 'unaffected')

    identity: dict[str, Any] = {
        'schema_version': IMPACT_SCHEMA_VERSION,
        'authority_version': IMPACT_AUTHORITY_VERSION,
        'document_id': to_revision.document_id,
        'from_revision_id': from_revision.revision_id,
        'from_content_hash': from_revision.content_hash,
        'to_revision_id': to_revision.revision_id,
        'to_content_hash': to_revision.content_hash,
        'changes': [item.model_dump(mode='json') for item in changes],
        'changed_axes': sorted(axes),
        'impacts': [item.model_dump(mode='json') for item in impacts],
    }
    digest = _digest(identity)
    return DependencyImpactReport(
        document_id=to_revision.document_id,
        from_revision_id=from_revision.revision_id,
        from_content_hash=from_revision.content_hash,
        to_revision_id=to_revision.revision_id,
        to_content_hash=to_revision.content_hash,
        changes=changes,
        changed_axes=axes,
        impacts=tuple(impacts),
        stale_count=stale,
        uncertain_count=uncertain,
        unaffected_count=unaffected,
        report_id=_semantic_id('impact-report', digest),
        report_sha256=digest,
    )
