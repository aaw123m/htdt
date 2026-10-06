from __future__ import annotations

from hashlib import sha256
import json
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .cad_scene import SceneDocument, scene_content_hash
from .capture_mission import (
    CaptureMission,
    CaptureMissionPackage,
    MissionTask,
    MissionTaskDependencyRef,
    _canonical_json,
    _deterministic_uuid,
    entity_identity_fingerprint,
    entity_pose_fingerprint,
    room_geometry_fingerprint,
)
from .project_identity import HTDTProjectReference


class MissionReconciliationError(ValueError):
    """Returned evidence could not be reconciled safely."""


BaselineDriftClass = Literal[
    'applicable',
    'needs_reconciliation',
    'historical_target_removed',
    'unaffected',
]


class TaskDriftResult(BaseModel):
    """Per-task baseline-drift classification for one returned fulfillment."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    task_id: str = Field(
        pattern=(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}'
            r'-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
        )
    )
    classification: BaselineDriftClass
    mission_target_id: str | None = None
    reason: str = Field(min_length=1)
    drifted_dependencies: tuple[str, ...] = ()
    missing_dependencies: tuple[str, ...] = ()


class MissionReconciliationReport(BaseModel):
    """Project-side disposition of returned evidence vs issuing baseline.

    This never rewrites the Capture-side record: mission id, plan hash,
    planned targets and observations stay bound to the exact mission that
    produced them. The report is the HTDT project-side reconciliation
    decision surface only.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema: Literal['htdt.capture.mission-reconciliation'] = (
        'htdt.capture.mission-reconciliation'
    )
    report_version: Literal[1] = 1
    mission_id: str
    plan_sha256: str
    baseline_scene_content_sha256: str
    current_scene_content_sha256: str
    results: tuple[TaskDriftResult, ...]

    @property
    def applicable_count(self) -> int:
        return sum(
            item.classification in {'applicable', 'unaffected'}
            for item in self.results
        )

    @property
    def review_count(self) -> int:
        return sum(
            item.classification == 'needs_reconciliation'
            for item in self.results
        )


def _dependency_fingerprint_current(
    dependency: MissionTaskDependencyRef,
    current: SceneDocument,
) -> str | None:
    """Recompute one pinned dependency against the current document.

    ``None`` means the referenced authority no longer exists in the current
    document — the evidence stays valid historically but must not auto-promote
    to the current intended topology.
    """

    if dependency.kind in {'entity_pose', 'entity_identity', 'equipment_slot'}:
        try:
            entity = current.entity(dependency.target_id)
        except KeyError:
            return None
        if dependency.kind == 'entity_pose':
            return entity_pose_fingerprint(entity)
        return entity_identity_fingerprint(entity)
    if dependency.kind == 'room_geometry':
        if current.document_id != dependency.target_id:
            return None
        return room_geometry_fingerprint(current)
    # Datum/tolerance/measurement-plan authorities are pinned references the
    # current document cannot recompute: they are treated as still pinned —
    # drift on them is surfaced by the caller supplying an explicit
    # ``superseded_authority_ids`` set rather than by guessing.
    return dependency.fingerprint


def reconcile_mission_return(
    mission: CaptureMission,
    current: SceneDocument,
    *,
    returned_task_ids: Sequence[str] | None = None,
    superseded_authority_ids: Sequence[str] = (),
) -> MissionReconciliationReport:
    """Classify each returned fulfillment against baseline vs current state.

    - ``applicable`` / ``unaffected``: evidence can be used under current
      project authority as-is.
    - ``needs_reconciliation``: the mission baseline and current design differ
      on a dependency this task pinned; requires an explicit reconciliation
      decision — never a silent rebind.
    - ``historical_target_removed``: the pinned target no longer exists; the
      evidence remains historical field evidence.

    ``returned_task_ids`` restricts evaluation to the tasks the return actually
    fulfilled; omitted means every planned task. Tasks with no returned
    evidence are simply not evaluated — they are not classified stale.
    """

    superseded = set(superseded_authority_ids)
    requested = (
        set(returned_task_ids)
        if returned_task_ids is not None
        else {task.task_id for task in mission.plan.tasks}
    )
    tasks_by_id = {task.task_id: task for task in mission.plan.tasks}
    unknown = requested - set(tasks_by_id)
    if unknown:
        raise MissionReconciliationError(
            f'return references tasks not in mission plan: {sorted(unknown)}'
        )

    results: list[TaskDriftResult] = []
    for task_id in sorted(requested):
        task = tasks_by_id[task_id]
        results.append(_classify_task(task, current, superseded))
    return MissionReconciliationReport(
        mission_id=mission.mission_id,
        plan_sha256=mission.plan.plan_sha256,
        baseline_scene_content_sha256=mission.baseline.scene_content_sha256,
        current_scene_content_sha256=scene_content_hash(current),
        results=tuple(results),
    )


def _classify_task(
    task: MissionTask,
    current: SceneDocument,
    superseded: set[str],
) -> TaskDriftResult:
    missing: list[str] = []
    drifted: list[str] = []
    for dependency in task.dependencies:
        target = f'{dependency.kind}:{dependency.target_id}'
        if dependency.target_id in superseded or target in superseded:
            missing.append(target)
            continue
        current_fingerprint = _dependency_fingerprint_current(
            dependency,
            current,
        )
        if current_fingerprint is None:
            missing.append(target)
        elif current_fingerprint != dependency.fingerprint:
            drifted.append(target)

    if missing:
        return TaskDriftResult(
            task_id=task.task_id,
            classification='historical_target_removed',
            mission_target_id=task.target_entity_id,
            reason=(
                'baseline dependency no longer exists in current project; '
                'evidence remains historical'
            ),
            missing_dependencies=tuple(missing),
            drifted_dependencies=tuple(drifted),
        )
    if drifted:
        return TaskDriftResult(
            task_id=task.task_id,
            classification='needs_reconciliation',
            mission_target_id=task.target_entity_id,
            reason=(
                'baseline dependency fingerprint differs under current '
                'project; explicit reconciliation decision required'
            ),
            drifted_dependencies=tuple(drifted),
        )
    if not task.dependencies:
        return TaskDriftResult(
            task_id=task.task_id,
            classification='unaffected',
            mission_target_id=task.target_entity_id,
            reason='task pins no baseline dependency',
        )
    return TaskDriftResult(
        task_id=task.task_id,
        classification='applicable',
        mission_target_id=task.target_entity_id,
        reason='all pinned baseline dependencies match current project',
    )


class RebaseDecision(BaseModel):
    """Explicit recorded decision to reuse returned evidence under current
    authority — a project-side decision, never a mutation of the source."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema: Literal['htdt.capture.reconciliation-decision'] = (
        'htdt.capture.reconciliation-decision'
    )
    decision_version: Literal[1] = 1
    decision_id: str
    mission_id: str = Field(min_length=1)
    plan_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    task_id: str
    source_target_id: str = Field(min_length=1)
    current_target_id: str = Field(min_length=1)
    mapping_reason: str = Field(min_length=1)
    decided_by: str = Field(min_length=1)
    resulting_authority: str | None = Field(default=None, min_length=1)


def record_rebase_decision(
    *,
    mission: CaptureMission,
    task: MissionTask,
    current_target_id: str,
    mapping_reason: str,
    decided_by: str,
    resulting_authority: str | None = None,
) -> RebaseDecision:
    """Persist an explicit rebase/reconciliation decision.

    Rebase is only meaningful for drifted evidence: the task must carry a
    pinned source target, and the caller must name the exact current target —
    mapping by display name or nearest position is impossible here because
    both endpoints are exact ids.
    """

    if task.target_entity_id is None:
        raise MissionReconciliationError(
            'cannot rebase a task with no pinned entity target'
        )
    decision_id = _deterministic_uuid(
        'htdt.capture.reconciliation-decision.v1',
        mission.mission_sha256,
        task.task_id,
        task.target_entity_id,
        current_target_id,
        mapping_reason,
    )
    return RebaseDecision(
        decision_id=decision_id,
        mission_id=mission.mission_id,
        plan_sha256=mission.plan.plan_sha256,
        task_id=task.task_id,
        source_target_id=task.target_entity_id,
        current_target_id=current_target_id,
        mapping_reason=mapping_reason,
        decided_by=decided_by,
        resulting_authority=resulting_authority,
    )


def outstanding_mission_drift_summary(
    mission: CaptureMission,
    current: SceneDocument,
) -> dict[str, int | bool]:
    """Overview surface: does current project drift affect this mission?

    Informational only — an outstanding mission never blocks ordinary design
    edits, and Capture continues executing the exact issued plan offline.
    """

    report = reconcile_mission_return(mission, current)
    return {
        'has_drift': (
            report.baseline_scene_content_sha256
            != report.current_scene_content_sha256
        ),
        'applicable': report.applicable_count,
        'needs_reconciliation': report.review_count,
        'historical_target_removed': sum(
            item.classification == 'historical_target_removed'
            for item in report.results
        ),
    }


def mission_return_reconciliation_lines(
    contribution: object,
    scene_repository: object,
) -> tuple[str, ...]:
    """Detail-pane summary lines for one staged field-return contribution.

    Classification runs live against the matched project's current head —
    a staged contribution is evidence, so the drift verdict is a decision
    surface computed on demand, never a stored rewrite. Returns display
    lines (empty for contributions carrying no mission identity); every
    un-reconcilable state surfaces as an honest note instead of silence.
    """
    from contextlib import closing

    from .cad_schema import connect_sqlite
    from .field_return_ingestion import FieldReturnManifest

    mission_id = getattr(contribution, 'mission_id', None)
    if mission_id is None:
        return ()
    path = scene_repository.path
    with closing(connect_sqlite(path)) as connection:
        row = connection.execute(
            'SELECT native_package_json FROM capture_mission_packages '
            'WHERE package_id=?',
            (mission_id,),
        ).fetchone()
        if row is None or row['native_package_json'] is None:
            return (
                '照合: ミッション原本なし'
                '（このHTDTが発行したミッションではありません）',
            )
        try:
            package = CaptureMissionPackage.model_validate_json(
                row['native_package_json']
            )
        except ValueError:
            return ('照合: ミッション原本を解読できません',)
        matched_project_id = getattr(
            contribution, 'matched_project_id', None
        )
        if matched_project_id is None:
            project_row = None
        else:
            project_row = connection.execute(
                'SELECT document_id FROM htdt_project_documents '
                'WHERE project_id=?',
                (matched_project_id,),
            ).fetchone()
    if project_row is None:
        return ('照合: 保存先プロジェクト未確定のため保留',)
    revision = scene_repository.current_head(project_row['document_id'])
    if revision is None:
        return ('照合: プロジェクト文書の現在版がありません',)
    manifest_json = getattr(contribution, 'manifest_json', None)
    if manifest_json is None:
        return ('照合: 返却マニフェストが記録されていません',)
    try:
        manifest = FieldReturnManifest.model_validate_json(manifest_json)
    except ValueError:
        return ('照合: 返却マニフェストを解読できません',)
    returned_ids = [
        outcome.task_id
        for outcome in manifest.task_outcomes
        if outcome.outcome == 'fulfilled'
    ]
    try:
        report = reconcile_mission_return(
            package.mission,
            revision.document,
            returned_task_ids=returned_ids,
        )
    except MissionReconciliationError as exc:
        return (f'照合: 照合不能 — {exc}',)
    removed = sum(
        item.classification == 'historical_target_removed'
        for item in report.results
    )
    lines = [
        f'照合: 適用可能 {report.applicable_count}件 / '
        f'要調整 {report.review_count}件 / 対象消失 {removed}件',
    ]
    lines.extend(
        f'・{result.task_id[:8]}… — {result.reason}'
        for result in report.results
        if result.classification
        in ('needs_reconciliation', 'historical_target_removed')
    )
    return tuple(lines)
