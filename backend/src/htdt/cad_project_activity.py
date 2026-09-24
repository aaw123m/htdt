"""Project lifecycle / activity timeline (#615).

Individual authorities already carry strong timestamps and lineage, but they
are spread across repositories. This module projects a read-only
``ProjectActivityEvent`` stream answering "what changed in this theater, in
human terms, and when?" — derived entirely from canonical authority plus
explicit user notes, never from a mutable hand-written audit log.

Contract properties:

- events are a *rebuildable projection*: each event's id is a deterministic
  digest of its source authority, so rebuilding from canonical records
  produces the identical set and dedupe needs no stored state;
- the projection is distinct from SceneRevision history (#485 — how the
  Scene evolved), the authority graph (#590 — causal provenance) and the
  Activity Center (#603 — transient/running app operations);
- routine UI actions and cache operations never emit events — only durable
  project authority does;
- events deep-link to the exact surviving authority via
  ``htdt://workspace/...`` URIs;
- user milestone notes (:class:`ProjectActivityNote`) are documentation and
  can never become measured/as-built truth;
- events copied with a duplicated/imported project keep ``inherited=True``
  so they read as "inherited from source project", never as if they
  happened again at copy time.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Iterable, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_repository import SceneRepository
from .workflow_navigation import WorkspaceDeepLink, WorkspaceId


ACTIVITY_EVENT_KINDS: frozenset[str] = frozenset(
    {
        'project_created',
        'project_note',
        'scene_revision_saved',
        'scene_revision_labeled',
        'system_variant_proposed',
        'system_variant_applied',
        'system_variant_as_built',
        'system_variant_measured',
        'capture_staged',
        'capture_promoted',
        'capture_rejected',
        'measurement_imported',
        'calibration_plan_created',
        'calibration_exported',
        'calibration_applied',
        'calibration_remeasured',
        'calibration_validated',
        'design_checkpoint_created',
        'design_checkpoint_restored',
        'operating_preset_created',
        'operating_preset_applied',
        'health_baseline_created',
        'health_check_completed',
        'av_sync_recorded',
        'other_authority',
    }
)

ActivityEventKind = Literal[
    'project_created',
    'project_note',
    'scene_revision_saved',
    'scene_revision_labeled',
    'system_variant_proposed',
    'system_variant_applied',
    'system_variant_as_built',
    'system_variant_measured',
    'capture_staged',
    'capture_promoted',
    'capture_rejected',
    'measurement_imported',
    'calibration_plan_created',
    'calibration_exported',
    'calibration_applied',
    'calibration_remeasured',
    'calibration_validated',
    'design_checkpoint_created',
    'design_checkpoint_restored',
    'operating_preset_created',
    'operating_preset_applied',
    'health_baseline_created',
    'health_check_completed',
    'av_sync_recorded',
    'other_authority',
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


def _link(workspace: WorkspaceId, context: str | None = None, entity_id: str | None = None) -> str:
    return WorkspaceDeepLink(
        workspace=workspace, section=context, entity_id=entity_id
    ).as_uri()


class ActivitySourceRef(BaseModel):
    """Pointer from one event back to the authority it was projected from."""

    model_config = ConfigDict(frozen=True)

    kind: str = Field(min_length=1)
    ref_id: str = Field(min_length=1)
    ref_sha256: str | None = Field(default=None, min_length=8)


class ProjectActivityEvent(BaseModel):
    """One projected timeline row; id is deterministic from its source."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    occurred_at_utc: str = Field(min_length=1)
    kind: ActivityEventKind
    title: str = Field(min_length=1)
    detail: str | None = None
    source_refs: tuple[ActivitySourceRef, ...] = ()
    deep_link: str | None = None
    inherited: bool = False
    event_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_event(self) -> 'ProjectActivityEvent':
        if self.event_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ProjectActivityEvent hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'event_id': self.event_id,
            'document_id': self.document_id,
            'occurred_at_utc': self.occurred_at_utc,
            'kind': self.kind,
            'title': self.title,
            'detail': self.detail,
            'source_refs': [item.model_dump(mode='json') for item in self.source_refs],
            'deep_link': self.deep_link,
            'inherited': self.inherited,
        }


class ProjectActivityNote(BaseModel):
    """User milestone/documentation note — never measured/as-built truth."""

    model_config = ConfigDict(frozen=True)

    note_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    title: str = Field(min_length=1)
    body: str | None = None
    note_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_note(self) -> 'ProjectActivityNote':
        if self.note_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ProjectActivityNote hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'note_id': self.note_id,
            'document_id': self.document_id,
            'created_at_utc': self.created_at_utc,
            'title': self.title,
            'body': self.body,
        }


def build_activity_note(
    *,
    document_id: str,
    title: str,
    body: str | None = None,
    created_at_utc: str,
    note_id: str | None = None,
) -> ProjectActivityNote:
    payload: dict[str, Any] = {
        'note_id': note_id or str(uuid4()),
        'document_id': document_id,
        'created_at_utc': created_at_utc,
        'title': title,
        'body': body,
    }
    provisional = ProjectActivityNote.model_construct(
        **payload, note_sha256='0' * 64
    )
    return ProjectActivityNote(
        **payload,
        note_sha256=_hash(provisional.semantic_payload()),
    )


def _event(
    *,
    document_id: str,
    kind: ActivityEventKind,
    source_kind: str,
    source_id: str,
    occurred_at_utc: str,
    title: str,
    detail: str | None = None,
    source_sha256: str | None = None,
    deep_link: str | None = None,
    inherited: bool = False,
    extra_refs: tuple[ActivitySourceRef, ...] = (),
) -> ProjectActivityEvent:
    """Build one event whose id is a deterministic digest of its source."""

    event_id = f'activity:{_hash({"kind": kind, "source_id": source_id})}'
    refs = (
        ActivitySourceRef(kind=source_kind, ref_id=source_id, ref_sha256=source_sha256),
        *extra_refs,
    )
    payload: dict[str, Any] = {
        'event_id': event_id,
        'document_id': document_id,
        'occurred_at_utc': occurred_at_utc,
        'kind': kind,
        'title': title,
        'detail': detail,
        'source_refs': refs,
        'deep_link': deep_link,
        'inherited': inherited,
    }
    provisional = ProjectActivityEvent.model_construct(
        **payload, event_sha256='0' * 64
    )
    return ProjectActivityEvent(
        **payload,
        event_sha256=_hash(provisional.semantic_payload()),
    )


class _ListRevisions(Protocol):
    def list_revisions(self, document_id: str) -> tuple[Any, ...]: ...
    def revision_labels(self, document_id: str) -> dict[str, Any]: ...


class CadProjectActivityService:
    """Rebuildable projection of cross-workspace project history.

    Every source is optional: a document gets a timeline from whichever
    authorities exist. The projection is a pure read — calling it twice on
    the same stores yields identical ``event_id``\\ s, and nothing here is
    itself authority.
    """

    def __init__(
        self,
        *,
        scene_repository: SceneRepository,
        variant_repository: Any | None = None,
        variant_lifecycle_repository: Any | None = None,
        calibration_repository: Any | None = None,
        capture_inbox: Any | None = None,
        measurement_repository: Any | None = None,
        checkpoint_repository: Any | None = None,
        preset_repository: Any | None = None,
        health_repository: Any | None = None,
        av_sync_repository: Any | None = None,
        notes_repository: Any | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.variant_repository = variant_repository
        self.variant_lifecycle_repository = variant_lifecycle_repository
        self.calibration_repository = calibration_repository
        self.capture_inbox = capture_inbox
        self.measurement_repository = measurement_repository
        self.checkpoint_repository = checkpoint_repository
        self.preset_repository = preset_repository
        self.health_repository = health_repository
        self.av_sync_repository = av_sync_repository
        self.notes_repository = notes_repository

    # ------------------------------------------------------------------

    def events(self, document_id: str) -> tuple[ProjectActivityEvent, ...]:
        """Full chronological projection for one document."""

        events: list[ProjectActivityEvent] = []
        events.extend(self._scene_events(document_id))
        events.extend(self._variant_events(document_id))
        events.extend(self._capture_events(document_id))
        events.extend(self._measurement_events(document_id))
        events.extend(self._calibration_events(document_id))
        events.extend(self._checkpoint_events(document_id))
        events.extend(self._preset_events(document_id))
        events.extend(self._health_events(document_id))
        events.extend(self._av_sync_events(document_id))
        events.extend(self._note_events(document_id))
        events.sort(key=lambda item: (item.occurred_at_utc, item.event_id))
        return tuple(events)

    def recent(
        self,
        document_id: str,
        *,
        limit: int = 8,
    ) -> tuple[ProjectActivityEvent, ...]:
        """Newest events first — the Overview 'Recent activity' surface."""
        return tuple(reversed(self.events(document_id)))[:limit]

    def add_note(
        self,
        *,
        document_id: str,
        title: str,
        body: str | None = None,
        created_at_utc: str,
    ) -> ProjectActivityNote:
        """Persist a user milestone note; documentation, never evidence."""
        if self.notes_repository is None:
            raise ValueError('activity notes repository is not wired')
        note = build_activity_note(
            document_id=document_id,
            title=title,
            body=body,
            created_at_utc=created_at_utc,
        )
        self.notes_repository.save_note(note)
        return note

    # ------------------------------------------------------------------
    # Sources

    def _scene_events(self, document_id: str) -> Iterable[ProjectActivityEvent]:
        revisions = self.scene_repository.list_revisions(document_id)
        committed = [item for item in revisions if not item.detached]
        first_revision_id = committed[0].revision_id if committed else None
        for index, revision in enumerate(revisions):
            kind: ActivityEventKind = 'scene_revision_saved'
            title = f'部屋リビジョン R{index + 1} を保存'
            if revision.revision_id == first_revision_id:
                kind = 'project_created'
                title = 'プロジェクト作成（初回リビジョン）'
            yield _event(
                document_id=document_id,
                kind=kind,
                source_kind='scene_revision',
                source_id=revision.revision_id,
                source_sha256=revision.content_hash,
                occurred_at_utc=revision.created_at_utc,
                title=title,
                detail='非ヘッド履歴' if revision.detached else None,
                deep_link=_link(WorkspaceId.ROOM, 'history', revision.revision_id),
            )
        try:
            labels = self.scene_repository.revision_labels(document_id)
        except AttributeError:
            labels = {}
        for revision_id, label in sorted(labels.items(), key=lambda item: item[1].updated_at_utc):
            yield _event(
                document_id=document_id,
                kind='scene_revision_labeled',
                source_kind='revision_label',
                source_id=revision_id,
                occurred_at_utc=label.updated_at_utc,
                title=f'リビジョンにラベル「{label.label}」',
                detail=label.note or None,
                deep_link=_link(WorkspaceId.ROOM, 'history', revision_id),
            )

    def _variant_events(self, document_id: str) -> Iterable[ProjectActivityEvent]:
        if self.variant_repository is None:
            return
        for variant in self.variant_repository.list_variants(document_id):
            yield _event(
                document_id=document_id,
                kind='system_variant_proposed',
                source_kind='system_variant',
                source_id=variant.variant_id,
                source_sha256=variant.variant_sha256,
                occurred_at_utc=variant.created_at_utc,
                title=f'システムバリアント「{variant.name}」を提案',
                deep_link=_link(WorkspaceId.OPTIMIZATION, 'candidates', variant.variant_id),
            )
            application = self.variant_repository.application_for_variant(
                variant.variant_id
            )
            if application is not None:
                yield _event(
                    document_id=document_id,
                    kind='system_variant_applied',
                    source_kind='system_variant_application',
                    source_id=application.application_id,
                    source_sha256=application.application_sha256,
                    occurred_at_utc=application.selected_at_utc,
                    title=f'バリアント「{variant.name}」を適用',
                    deep_link=_link(
                        WorkspaceId.ROOM, 'history', application.applied_revision_id
                    ),
                    extra_refs=(
                        ActivitySourceRef(
                            kind='system_variant',
                            ref_id=variant.variant_id,
                            ref_sha256=variant.variant_sha256,
                        ),
                    ),
                )
                if self.variant_lifecycle_repository is not None:
                    as_built = self.variant_lifecycle_repository.for_application(
                        application.application_id
                    )
                    if as_built is not None:
                        yield _event(
                            document_id=document_id,
                            kind='system_variant_as_built',
                            source_kind='system_variant_as_built',
                            source_id=as_built.record_id,
                            source_sha256=as_built.record_sha256,
                            occurred_at_utc=as_built.confirmed_at_utc,
                            title=f'バリアント「{variant.name}」を As-built として記録',
                            deep_link=_link(
                                WorkspaceId.ROOM, 'history', as_built.as_built_revision_id
                            ),
                        )

    def _capture_events(self, document_id: str) -> Iterable[ProjectActivityEvent]:
        if self.capture_inbox is None:
            return
        for item in self.capture_inbox.list_items():
            yield _event(
                document_id=document_id,
                kind='capture_staged',
                source_kind='capture_inbox_item',
                source_id=item.inbox_item_id,
                occurred_at_utc=item.first_arrived_at_utc,
                title=f'キャプチャリビジョン {item.capture_revision_id} を受信',
                detail=f'バンドル {item.bundle_digest[:12]}…',
                deep_link=_link(WorkspaceId.MEASUREMENT, 'import', item.inbox_item_id),
            )
            if item.disposition == 'rejected' and item.disposition_at_utc is not None:
                yield _event(
                    document_id=document_id,
                    kind='capture_rejected',
                    source_kind='capture_inbox_item',
                    source_id=f'{item.inbox_item_id}:rejected',
                    occurred_at_utc=item.disposition_at_utc,
                    title=f'キャプチャ {item.capture_revision_id} を却下',
                    detail=item.disposition_reason or None,
                    deep_link=_link(WorkspaceId.MEASUREMENT, 'import', item.inbox_item_id),
                )
            elif item.disposition in {'promoted', 'partially_promoted'} and (
                item.disposition_at_utc is not None
            ):
                yield _event(
                    document_id=document_id,
                    kind='capture_promoted',
                    source_kind='capture_inbox_item',
                    source_id=f'{item.inbox_item_id}:{item.disposition}',
                    occurred_at_utc=item.disposition_at_utc,
                    title=f'キャプチャ {item.capture_revision_id} を昇格',
                    detail=item.disposition_reason or None,
                    deep_link=_link(WorkspaceId.MEASUREMENT, 'import', item.inbox_item_id),
                )

    def _measurement_events(self, document_id: str) -> Iterable[ProjectActivityEvent]:
        if self.measurement_repository is None:
            return
        for measurement in self.measurement_repository.list_measurements(document_id):
            detail = None
            if measurement.captured_at is not None:
                detail = f'取得 {measurement.captured_at} / 取込 {measurement.imported_at}'
            yield _event(
                document_id=document_id,
                kind='measurement_imported',
                source_kind='measurement',
                source_id=measurement.measurement_id,
                occurred_at_utc=measurement.imported_at,
                title='測定をインポート',
                detail=detail,
                deep_link=_link(
                    WorkspaceId.MEASUREMENT, 'comparison', measurement.measurement_id
                ),
            )

    def _calibration_events(self, document_id: str) -> Iterable[ProjectActivityEvent]:
        if self.calibration_repository is None:
            return
        for plan in self.calibration_repository.list_plans(document_id):
            yield _event(
                document_id=document_id,
                kind='calibration_plan_created',
                source_kind='calibration_plan',
                source_id=plan.plan_id,
                source_sha256=plan.plan_semantic_sha256,
                occurred_at_utc=plan.created_at_utc,
                title='キャリブレーション計画を作成',
                deep_link=_link(WorkspaceId.MEASUREMENT, 'calibration', plan.plan_id),
            )
            for export in self.calibration_repository.list_exports(plan.plan_id):
                yield _event(
                    document_id=document_id,
                    kind='calibration_exported',
                    source_kind='calibration_export',
                    source_id=export.export_id,
                    source_sha256=export.exported_settings_semantic_sha256,
                    occurred_at_utc=export.created_at_utc,
                    title='キャリブレーション設定を出力',
                    deep_link=_link(WorkspaceId.MEASUREMENT, 'calibration', plan.plan_id),
                )
            for event in self.calibration_repository.list_lifecycle_events(plan.plan_id):
                kind_map: dict[str, ActivityEventKind] = {
                    'user_applied': 'calibration_applied',
                    'remeasured': 'calibration_remeasured',
                    'validated': 'calibration_validated',
                }
                kind = kind_map.get(event.state)
                if kind is None:
                    continue
                yield _event(
                    document_id=document_id,
                    kind=kind,
                    source_kind='calibration_lifecycle_event',
                    source_id=event.event_id,
                    source_sha256=event.event_semantic_sha256,
                    occurred_at_utc=event.created_at_utc,
                    title={
                        'user_applied': 'キャリブレーション設定を実機へ適用',
                        'remeasured': 'キャリブレーション再測定を記録',
                        'validated': 'キャリブレーションを検証済みに更新',
                    }[event.state],
                    detail=event.note,
                    deep_link=_link(WorkspaceId.MEASUREMENT, 'calibration', plan.plan_id),
                )

    def _checkpoint_events(self, document_id: str) -> Iterable[ProjectActivityEvent]:
        if self.checkpoint_repository is None:
            return
        for checkpoint in self.checkpoint_repository.list_checkpoints(document_id):
            yield _event(
                document_id=document_id,
                kind='design_checkpoint_created',
                source_kind='design_checkpoint',
                source_id=checkpoint.checkpoint_id,
                source_sha256=checkpoint.checkpoint_sha256,
                occurred_at_utc=checkpoint.created_at_utc,
                title=f'設計チェックポイント「{checkpoint.title}」を作成',
                detail=checkpoint.note,
                deep_link=_link(WorkspaceId.OVERVIEW, None, checkpoint.checkpoint_id),
            )
        for restore in self.checkpoint_repository.list_restores(document_id):
            yield _event(
                document_id=document_id,
                kind='design_checkpoint_restored',
                source_kind='checkpoint_restore',
                source_id=restore.restore_id,
                source_sha256=restore.restore_sha256,
                occurred_at_utc=restore.created_at_utc,
                title='設計チェックポイントを復元',
                detail=' / '.join(restore.applied_components),
                deep_link=_link(WorkspaceId.OVERVIEW, None, restore.checkpoint_id),
            )

    def _preset_events(self, document_id: str) -> Iterable[ProjectActivityEvent]:
        if self.preset_repository is None:
            return
        for preset in self.preset_repository.list_presets(document_id):
            yield _event(
                document_id=document_id,
                kind='operating_preset_created',
                source_kind='operating_preset',
                source_id=preset.preset_id,
                source_sha256=preset.preset_sha256,
                occurred_at_utc=preset.created_at_utc,
                title=f'運用プリセット「{preset.name}」を作成',
                detail=preset.purpose_note,
                deep_link=_link(WorkspaceId.OVERVIEW, None, preset.preset_id),
            )
            for applied in self.preset_repository.list_applied_states(preset.preset_id):
                yield _event(
                    document_id=document_id,
                    kind='operating_preset_applied',
                    source_kind='applied_preset_state',
                    source_id=applied.applied_id,
                    source_sha256=applied.applied_sha256,
                    occurred_at_utc=applied.confirmed_at_utc,
                    title=f'プリセット「{preset.name}」を実機へ適用と記録',
                    detail=applied.device_context,
                    deep_link=_link(WorkspaceId.OVERVIEW, None, preset.preset_id),
                )

    def _health_events(self, document_id: str) -> Iterable[ProjectActivityEvent]:
        if self.health_repository is None:
            return
        for baseline in self.health_repository.list_baselines(document_id):
            yield _event(
                document_id=document_id,
                kind='health_baseline_created',
                source_kind='health_baseline',
                source_id=baseline.baseline_id,
                source_sha256=baseline.baseline_sha256,
                occurred_at_utc=baseline.created_at_utc,
                title=f'健全性ベースライン「{baseline.name}」を固定',
                deep_link=_link(WorkspaceId.OVERVIEW, None, baseline.baseline_id),
            )
        for run in self.health_repository.list_document_runs(document_id):
            changed = sum(1 for item in run.assessments if item.state == 'changed')
            detail = f'{changed} 件の変化を検出' if changed else '変化なし'
            yield _event(
                document_id=document_id,
                kind='health_check_completed',
                source_kind='health_check_run',
                source_id=run.run_id,
                source_sha256=run.run_sha256,
                occurred_at_utc=run.created_at_utc,
                title='健全性チェックを実施',
                detail=detail,
                deep_link=_link(WorkspaceId.OVERVIEW, None, run.plan_id),
            )

    def _av_sync_events(self, document_id: str) -> Iterable[ProjectActivityEvent]:
        if self.av_sync_repository is None:
            return
        for condition in self.av_sync_repository.list_conditions(document_id):
            for measurement in self.av_sync_repository.list_measurements(
                condition.condition_id
            ):
                yield _event(
                    document_id=document_id,
                    kind='av_sync_recorded',
                    source_kind='av_sync_measurement',
                    source_id=measurement.measurement_id,
                    source_sha256=measurement.measurement_sha256,
                    occurred_at_utc=measurement.captured_at,
                    title='AV同期を記録',
                    deep_link=_link(WorkspaceId.MEASUREMENT, 'quality', condition.condition_id),
                )

    def _note_events(self, document_id: str) -> Iterable[ProjectActivityEvent]:
        if self.notes_repository is None:
            return
        for note in self.notes_repository.list_notes(document_id):
            yield _event(
                document_id=document_id,
                kind='project_note',
                source_kind='project_note',
                source_id=note.note_id,
                source_sha256=note.note_sha256,
                occurred_at_utc=note.created_at_utc,
                title=f'メモ「{note.title}」',
                detail=note.body,
                deep_link=_link(WorkspaceId.OVERVIEW, None, note.note_id),
            )
